"""Provider routing is covered with fakes; no provider requests are made."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from hirevia.ai.client import (
    AIAuthError,
    AIError,
    AIInvalidResponseError,
    AIModelUnavailableError,
    AIRateLimitError,
    AIServerError,
    AITimeoutError,
    OpenAICompatibleClient,
    classify_api_error,
)
from hirevia.ai.matcher import AIMatcher, MATCH_RESPONSE_SCHEMA, SYSTEM_PROMPT
from hirevia.ai.providers import AIConfigurationError, AIProviderRouter, build_ai_provider_router, provider_status
from hirevia.config import AIConfig, Config, load_config
from hirevia.core.matching import analyze
from hirevia.core.models import CandidateProfile, Job


VALID_RESULT = {
    "match": True,
    "match_score": 81,
    "priority": "P2",
    "reason": "The Python role matches the candidate profile.",
}


class FakeProvider:
    def __init__(self, *outcomes, model="fake-model"):
        self.outcomes = list(outcomes)
        self.model = model
        self.calls = 0

    async def structured_completion(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def invoke(router):
    return asyncio.run(router.structured_completion(messages=[], schema={}))


def make_openai_fake(provider_name, content, model):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content)
        )])

    endpoint = "http://127.0.0.1:8080/v1" if provider_name == "llamacpp" else "https://example.test/v1"
    client = OpenAICompatibleClient(
        endpoint, model, "fake-api-key", provider_name=provider_name
    )
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client, calls


def test_llamacpp_simple_structured_request_parses_valid_json():
    provider, calls = make_openai_fake(
        "llamacpp", '{"match": true, "match_score": 81, "priority": "P2", "reason": "Python role."}',
        "hirevia-qwen3-1.7b",
    )

    result = asyncio.run(provider.structured_completion(
        messages=[{"role": "user", "content": "Reply with JSON."}],
        schema={"type": "object"}, retries=0,
    ))

    assert result["match_score"] == 81
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert provider.model == "hirevia-qwen3-1.7b"


def test_llamacpp_matching_request_shape_returns_parsed_verdict():
    content = '{"match": true, "match_score": 81, "priority": "P2", "reason": "Python role."}'
    provider, calls = make_openai_fake("llamacpp", content, "hirevia-qwen3-1.7b")
    router = AIProviderRouter({"llamacpp": provider}, "llamacpp", [])
    matcher = AIMatcher(Config(), provider=router)
    job = Job(
        source="diagnostic", source_job_id="one", title="Junior Python Developer",
        company="Example Co", location="Remote",
        description="Entry-level backend role using Python and SQL.",
        url="https://example.test/jobs/one",
        application_url="https://example.test/jobs/one/apply",
    )
    profile = CandidateProfile(skills=["Python", "SQL"], preferred_roles=["Python Developer"])

    response = asyncio.run(matcher.match(job, profile, analyze(job, profile)))

    assert response.status == "ok" and response.degraded is False
    assert response.raw_data["provider"] == "llamacpp"
    assert calls[0]["messages"][0]["content"] == SYSTEM_PROMPT
    assert "Junior Python Developer" in calls[0]["messages"][1]["content"]
    assert calls[0]["response_format"]["json_schema"]["schema"] == MATCH_RESPONSE_SCHEMA
    assert calls[0]["temperature"] == 0.1 and calls[0]["max_tokens"] == 1200


@pytest.mark.parametrize("content", [None, "not JSON"])
def test_nvidia_empty_or_malformed_content_is_a_model_response_error(content):
    provider, _ = make_openai_fake("nvidia", content, "openai/gpt-oss-20b")

    with pytest.raises(AIInvalidResponseError):
        asyncio.run(provider.structured_completion(
            messages=[{"role": "user", "content": "Return JSON."}],
            schema={"type": "object"}, retries=0,
        ))


def test_model_response_failure_falls_back_without_cooling_primary(caplog):
    llamacpp = FakeProvider(AIInvalidResponseError("empty completion"), VALID_RESULT)
    nvidia = FakeProvider(VALID_RESULT)
    router = AIProviderRouter(
        {"llamacpp": llamacpp, "nvidia": nvidia},
        "llamacpp", ["nvidia"], cooldown_seconds=10,
    )

    assert invoke(router) == VALID_RESULT
    assert "llamacpp" not in router._cooldown_until
    assert invoke(router) == VALID_RESULT
    assert llamacpp.calls == 2
    assert router.last_provider == "llamacpp"
    assert "temporarily unavailable" not in caplog.text


def test_primary_provider_success_does_not_call_fallback():
    primary = FakeProvider(VALID_RESULT, model="primary-model")
    fallback = FakeProvider(VALID_RESULT)
    router = AIProviderRouter({"nvidia": primary, "groq": fallback}, "nvidia", ["groq"])

    assert invoke(router) == VALID_RESULT
    assert primary.calls == 1 and fallback.calls == 0
    assert router.last_provider == "nvidia" and router.last_model == "primary-model"


def test_fallback_provider_returns_the_same_canonical_match_fields():
    job = Job(
        source="test", source_job_id="1", title="Python Developer", company="Acme",
        location="Pune", description="Python role", url="https://example.com/job/1",
    )
    profile = CandidateProfile(skills=["Python"])
    primary_matcher = AIMatcher(
        Config(), provider=AIProviderRouter(
            {"nvidia": FakeProvider(VALID_RESULT)}, "nvidia", []
        )
    )
    fallback_matcher = AIMatcher(
        Config(), provider=AIProviderRouter(
            {"nvidia": FakeProvider(AITimeoutError("timeout")),
             "groq": FakeProvider(VALID_RESULT)},
            "nvidia", ["groq"],
        )
    )

    primary = asyncio.run(primary_matcher.match(job, profile))
    fallback = asyncio.run(fallback_matcher.match(job, profile))

    assert (primary.relevant, primary.match_score, primary.priority, primary.reason) == (
        fallback.relevant, fallback.match_score, fallback.priority, fallback.reason,
    )
    assert fallback.raw_data["provider"] == "groq"


@pytest.mark.parametrize(
    "failure",
    [
        AITimeoutError("timeout"),
        AIRateLimitError("rate limited", retry_after=2),
        AIServerError("server error"),
        AIModelUnavailableError("model unavailable"),
        AIInvalidResponseError("bad response"),
    ],
)
def test_transient_and_unavailable_primary_failures_fall_back(failure):
    primary = FakeProvider(failure)
    fallback = FakeProvider(VALID_RESULT, model="groq-model")
    router = AIProviderRouter({"nvidia": primary, "groq": fallback}, "nvidia", ["groq"])

    assert invoke(router) == VALID_RESULT
    assert primary.calls == fallback.calls == 1
    assert router.last_provider == "groq" and router.last_model == "groq-model"


@pytest.mark.parametrize("failure", [AIAuthError("invalid key"), AIError("invalid request")])
def test_auth_and_configuration_errors_do_not_fall_back(failure):
    primary = FakeProvider(failure)
    fallback = FakeProvider(VALID_RESULT)
    router = AIProviderRouter({"nvidia": primary, "groq": fallback}, "nvidia", ["groq"])

    with pytest.raises(type(failure)):
        invoke(router)
    assert primary.calls == 1 and fallback.calls == 0


def test_every_provider_failure_returns_a_typed_failure_for_matcher_fallback():
    primary = FakeProvider(AITimeoutError("AI request did not complete (timed out)"))
    fallback = FakeProvider(AIServerError("server error"))
    router = AIProviderRouter({"nvidia": primary, "groq": fallback}, "nvidia", ["groq"])
    matcher = AIMatcher(Config(), provider=router)
    job = Job(
        source="test", source_job_id="1", title="Python Developer", company="Acme",
        location="Pune", description="Python role", url="https://example.com/job/1",
    )

    response = asyncio.run(matcher.match(job, CandidateProfile(skills=["Python"])))

    assert response.degraded
    assert "server error" in response.error
    assert response.raw_data["provider"] == "groq"


def test_provider_cooldown_skips_primary_until_expiry():
    now = [0.0]
    primary = FakeProvider(AITimeoutError("timeout"), VALID_RESULT)
    fallback = FakeProvider(VALID_RESULT)
    router = AIProviderRouter(
        {"nvidia": primary, "groq": fallback}, "nvidia", ["groq"],
        cooldown_seconds=10, clock=lambda: now[0],
    )

    invoke(router)
    now[0] = 1.0
    invoke(router)
    assert primary.calls == 1
    assert fallback.calls == 2

    now[0] = 11.0
    invoke(router)
    assert primary.calls == 2
    assert router.last_provider == "nvidia"


def test_unconfigured_providers_are_skipped_and_reported(monkeypatch, tmp_path):
    for name in ("NVIDIA_API_KEY", "GROQ_API_KEY", "OLLAMA_API_KEY", "LLAMACPP_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("hirevia.config._ensure_dotenv", lambda: None)
    config = load_config(base_dir=tmp_path)
    router = build_ai_provider_router(config)

    with pytest.raises(AIConfigurationError):
        invoke(router)
    assert all(not row["configured"] for row in provider_status(config))


def test_primary_provider_loads_from_environment(monkeypatch, tmp_path):
    monkeypatch.setattr("hirevia.config._ensure_dotenv", lambda: None)
    monkeypatch.setenv("AI_PRIMARY_PROVIDER", "llamacpp")

    config = load_config(base_dir=tmp_path)

    assert config.ai.primary == "llamacpp"
    router = AIProviderRouter({}, config.ai.primary, config.ai.fallback)
    assert router.fallback == ["nvidia", "groq", "ollama"]


def test_invalid_primary_endpoint_does_not_silently_use_fallback(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    config = Config()
    config.nvidia_base_url = "not-a-url"
    router = build_ai_provider_router(config)

    with pytest.raises(AIConfigurationError, match="invalid endpoint"):
        invoke(router)
    assert router.last_provider == "nvidia"


def test_status_does_not_expose_provider_secrets(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "groq-CANARY-do-not-print")
    rows = provider_status(Config())

    assert next(row for row in rows if row["name"] == "groq")["configured"]
    assert "groq-CANARY-do-not-print" not in repr(rows)


def test_provider_errors_do_not_echo_api_keys():
    canary = "secret-CANARY-never-log"
    typed = classify_api_error(
        RuntimeError(f"401 unauthorized; Authorization: Bearer {canary}"),
        provider_name="groq", api_key_env="GROQ_API_KEY",
    )

    assert isinstance(typed, AIError)
    assert canary not in str(typed)
    assert "GROQ_API_KEY" not in str(typed) or canary not in str(typed)


def test_reasoning_content_is_never_used_as_completion_or_error_text(caplog):
    client = OpenAICompatibleClient(
        "https://example.test/v1", "local-model", "test-key",
        provider_name="test-provider",
    )
    private_reasoning = "private-reasoning-CANARY"
    message = SimpleNamespace(content=None, reasoning_content=private_reasoning)
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: None))
    )

    async def fake_create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client.client.chat.completions.create = fake_create
    with pytest.raises(AIInvalidResponseError):
        asyncio.run(client.chat_completion(messages=[]))
    assert private_reasoning not in caplog.text


def test_nvidia_legacy_model_configuration_still_builds_primary(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test-key")
    monkeypatch.setenv("NVIDIA_MODEL", "openai/gpt-oss-20b")
    config = load_config()
    router = build_ai_provider_router(config)

    assert router.model == "openai/gpt-oss-20b"
    assert router.providers["nvidia"].provider_name == "nvidia"
