"""Phase 7 Stage 4: the AI matching layer (NVIDIA endpoint -> MatchResult).

Nothing here touches the network: the OpenAI-compatible transport is replaced by
a fake `chat.completions.create`, so every test drives the real code path
(`chat_completion` -> `classify_api_error` -> `structured_completion` ->
`AIResponse.from_json` -> `combine`) against a scripted endpoint.

The contract being pinned is the one the pipeline depends on:

  * a good answer arrives as a structured verdict, with the model recorded;
  * every failure mode (malformed JSON, empty answer, timeout, rate limit,
    4xx/5xx, retired model, missing/invalid fields) degrades into a
    `AIResponse(degraded=True)` - the matcher never raises;
  * retries happen only where retrying can help, and never for auth or a dead
    model name;
  * the API key never reaches a log line, an error string or stored payload;
  * the deterministic evidence really is in the request the model receives.
"""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

try:  # openai>=3.20 vendors httpx2; older builds use plain httpx
    import httpx2 as httpx
except ModuleNotFoundError:  # pragma: no cover - depends on the SDK build
    import httpx

from openai import APIStatusError, APITimeoutError, RateLimitError

from hirevia.ai.client import (
    AIAuthError,
    AIError,
    AIInvalidResponseError,
    AIModelUnavailableError,
    AIRateLimitError,
    AIServerError,
    AITimeoutError,
    NVIDIAClient,
    classify_api_error,
    extract_json_object,
)
from hirevia.ai.matcher import (
    MATCH_RESPONSE_SCHEMA,
    AIMatcher,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from hirevia.config import Config, load_config
from hirevia.core.matching import analyze, combine
from hirevia.core.models import AIResponse, CandidateProfile, Job, MatchResult, Priority

# A canary: this string must never appear in a log line, error message,
# stored payload or prompt anywhere in the AI layer.
API_KEY = "nvapi-CANARY-s3cr3t-never-log-me"
BASE_URL = "https://integrate.api.nvidia.com/v1"

VALID_PAYLOAD = {
    "match": True,
    "match_score": 82,
    "priority": "P2",
    "reason": "Python/FastAPI backend role that matches your stack and Pune location.",
    "matched_skills": ["Python", "FastAPI", "SQL"],
    "missing_skills": ["Kubernetes"],
    "experience_match": True,
    "location_match": True,
    "role_match": True,
}
VALID_JSON = json.dumps(VALID_PAYLOAD)


# --- helpers ------------------------------------------------------------------


def make_config(**overrides) -> Config:
    defaults = dict(nvidia_model="nvidia/test-model", nvidia_base_url=BASE_URL,
                    ai_timeout_seconds=5)
    defaults.update(overrides)
    return Config(**defaults)


def make_profile(**overrides) -> CandidateProfile:
    defaults = dict(
        name="Test User",
        candidate_type="fresher",
        experience_years=0,
        target_max_experience_years=2,
        accept_fresher=True,
        skills=["Python", "FastAPI", "SQL", "Machine Learning"],
        preferred_roles=["Python Developer", "Backend Developer"],
        preferred_locations=["Pune"],
        remote_preference="preferred",
        employment_types=["full_time", "internship"],
    )
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def make_job(**overrides) -> Job:
    defaults = dict(
        source="test",
        source_job_id="1",
        title="Python Developer",
        company="Acme Corp",
        location="Pune, Maharashtra",
        description="Python and FastAPI role. SQL queries. Fresher friendly, 0-1 years.",
        url="https://example.com/job/1",
        application_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    defaults.update(overrides)
    return Job(**defaults)


def completion(content):
    """The minimal shape `chat_completion` reads out of an SDK response."""
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def http_error(status, message="", headers=None, error_class=APIStatusError):
    """A real SDK error carrying a real HTTP status (what the gateway sends)."""
    request = httpx.Request("POST", BASE_URL + "/chat/completions")
    response = httpx.Response(status, request=request, headers=headers or {})
    return error_class(message or f"Error {status}", response=response, body=None)


class FakeCompletions:
    """Stands in for `client.chat.completions`.

    Each call consumes the next scripted outcome; the last outcome repeats, so a
    one-item list means "the endpoint always behaves like this".
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def wire(client, *outcomes):
    """Attach a fake transport to an NVIDIAClient and return the fake."""
    fake = FakeCompletions(outcomes)
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake.create))
    )
    return fake


def make_matcher(*outcomes, config=None):
    """A matcher whose endpoint always answers with `outcomes`."""
    matcher = AIMatcher(config or make_config(), API_KEY)
    return matcher, wire(matcher.client, *outcomes)


def ask(matcher, job=None, profile=None, evidence="default"):
    """Run one match() the way the pipeline does."""
    job = job or make_job()
    profile = profile or make_profile()
    if evidence == "default":
        evidence = analyze(job, profile)
    return asyncio.run(matcher.match(job, profile, evidence))


@pytest.fixture
def sleep_log(monkeypatch):
    """Records backoff instead of waiting for it, so retries stay instant."""
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("hirevia.ai.client.asyncio.sleep", fake_sleep)
    return delays


@pytest.fixture(autouse=True)
def ai_logs(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    return caplog


# --- 1. a valid NVIDIA response ----------------------------------------------


def test_valid_response_becomes_a_structured_verdict():
    matcher, fake = make_matcher(completion(VALID_JSON))

    response = ask(matcher)

    assert isinstance(response, AIResponse)
    assert response.degraded is False and response.status == "ok"
    assert response.relevant is True
    assert response.match_score == 82
    assert response.priority is Priority.P2
    assert response.matched_skills == ["Python", "FastAPI", "SQL"]
    assert response.missing_skills == ["Kubernetes"]
    assert response.role_match is True and response.location_match is True
    assert "Pune" in response.reason
    # traceability: which model said it, and the raw payload it said it with
    assert response.raw_data["model"] == "nvidia/test-model"
    assert response.raw_data["payload"]["match_score"] == 82
    assert len(fake.calls) == 1


def test_request_sends_the_configured_model_and_json_schema():
    matcher, fake = make_matcher(completion(VALID_JSON))

    ask(matcher)

    sent = fake.calls[0]
    assert sent["model"] == "nvidia/test-model"      # NVIDIA_MODEL, not hardcoded
    assert sent["temperature"] == 0.1
    assert sent["max_tokens"] == 600
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert sent["messages"][0]["content"] == SYSTEM_PROMPT
    schema = sent["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {"match", "match_score", "priority", "reason"}
    assert schema is MATCH_RESPONSE_SCHEMA


@pytest.mark.parametrize(
    "content",
    [
        "```json\n" + VALID_JSON + "\n```",              # fenced code block
        "```" + VALID_JSON + "```",                      # fence without a language
        "Here is the JSON you asked for:\n" + VALID_JSON,  # chatty preamble
        VALID_JSON + "\n\nHope this helps!",             # chatty trailer
    ],
)
def test_a_valid_object_inside_markdown_or_prose_is_recovered(content):
    assert extract_json_object(content)["match_score"] == 82

    matcher, _ = make_matcher(completion(content))
    assert ask(matcher).match_score == 82


# --- 2. malformed JSON --------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "I think this job is a great match for you!",           # prose, no object
        '{"match": true, "match_score":',                        # truncated object
        '{"match": true, "match_score": 82,}',                   # trailing comma
        "not json at all {{{",                                   # noise
    ],
)
def test_malformed_json_asks_once_more_then_degrades(content, sleep_log):
    matcher, fake = make_matcher(completion(content))

    response = ask(matcher)

    assert response.degraded is True and response.status == "failed"
    assert "unusable JSON" in response.error
    assert response.raw_data == {"model": "nvidia/test-model", "payload": {}}
    # retries=1 -> one repair round, and the repair round says what was wrong
    assert len(fake.calls) == 2
    assert sleep_log == []                       # a re-ask needs no backoff
    repair = fake.calls[1]["messages"]
    assert repair[-2]["role"] == "assistant" and repair[-2]["content"] == content
    assert "invalid JSON" in repair[-1]["content"]


def test_one_wrapped_verdict_is_unwrapped_but_a_list_of_them_is_not(sleep_log):
    """A model that wraps its single answer in `[...]` is still understood.

    Two answers in one list are ambiguous, and guessing which to trust is worse
    than degrading, so that path must fall back to the deterministic evidence.
    """
    matcher, _ = make_matcher(completion(json.dumps([VALID_PAYLOAD])))
    assert ask(matcher).match_score == 82

    ambiguous = json.dumps([VALID_PAYLOAD, dict(VALID_PAYLOAD, match_score=10)])
    matcher, _ = make_matcher(completion(ambiguous))
    response = ask(matcher)
    assert response.degraded is True
    assert response.match_score not in (82, 10)   # neither claim was trusted


def test_extract_json_object_refuses_prose_arrays_and_garbage():
    for bad in ["", "   ", "no object here", "[1, 2, 3]", '"just a string"', "42"]:
        with pytest.raises(json.JSONDecodeError):
            extract_json_object(bad)


# --- 3. empty response --------------------------------------------------------


def test_empty_completion_is_reported_not_retried_forever(sleep_log):
    matcher, fake = make_matcher(completion(""))

    response = ask(matcher)

    assert response.degraded is True
    assert "empty completion" in response.error
    assert "nvidia/test-model" in response.error   # says which model went quiet
    assert len(fake.calls) == 1                    # nothing to repair - no retry


def test_completion_with_no_choices_or_null_content_degrades():
    matcher, fake = make_matcher(SimpleNamespace(choices=[]), completion(None))

    assert ask(matcher).degraded is True     # an answer with no text is no answer
    assert len(fake.calls) == 1               # and it is not retried forever


def test_an_empty_code_fence_is_not_treated_as_a_verdict():
    with pytest.raises(json.JSONDecodeError):
        extract_json_object("```json\n\n```")


def test_the_client_raises_a_typed_error_rather_than_a_raw_sdk_one():
    """Callers must be able to tell 'the model said nothing' from 'auth failed'."""
    client = NVIDIAClient(make_config(), API_KEY)
    wire(client, completion(""))

    with pytest.raises(AIInvalidResponseError) as excinfo:
        asyncio.run(client.chat_completion(messages=[{"role": "user", "content": "hi"}]))
    assert "nvidia/test-model" in str(excinfo.value)


def test_extract_json_object_calls_out_an_empty_answer():
    with pytest.raises(json.JSONDecodeError) as excinfo:
        extract_json_object("")
    assert "empty response" in str(excinfo.value)


# --- 4. timeout ---------------------------------------------------------------


def test_timeout_is_a_typed_retryable_error():
    request = httpx.Request("POST", BASE_URL + "/chat/completions")
    typed = classify_api_error(APITimeoutError(request=request))

    assert isinstance(typed, AITimeoutError)
    assert "did not complete" in str(typed)


def test_timeout_retries_once_with_backoff_then_reports_degraded(sleep_log):
    request = httpx.Request("POST", BASE_URL + "/chat/completions")
    matcher, fake = make_matcher(APITimeoutError(request=request))

    response = ask(matcher)

    assert response.degraded is True and "timed out" in response.error.lower()
    assert len(fake.calls) == 2                    # exactly one retry, no storm
    assert sleep_log == [1.5]                      # bounded, predictable backoff


def test_timeout_that_recovers_on_the_second_try_yields_a_real_verdict(sleep_log):
    request = httpx.Request("POST", BASE_URL + "/chat/completions")
    matcher, _ = make_matcher(APITimeoutError(request=request), completion(VALID_JSON))

    response = ask(matcher)

    assert response.degraded is False and response.match_score == 82
    assert sleep_log == [1.5]


# --- 5. HTTP errors (5xx and other statuses) ----------------------------------


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_error_is_typed_as_transient(status):
    error = classify_api_error(http_error(status, "Internal server error"))

    assert isinstance(error, AIServerError)
    assert "AI server error" in str(error)


@pytest.mark.parametrize("message", ["Service Unavailable", "gateway unavailable"])
def test_http_503_unavailable_wording_remains_a_server_error(message):
    error = classify_api_error(http_error(503, message))

    assert isinstance(error, AIServerError)
    assert not isinstance(error, AIModelUnavailableError)


def test_transient_server_error_retries_with_timeout_backoff_then_recovers(sleep_log):
    matcher, fake = make_matcher(
        http_error(503, "Service Unavailable"), completion(VALID_JSON)
    )

    response = ask(matcher)

    assert response.degraded is False and response.match_score == 82
    assert len(fake.calls) == 2
    assert sleep_log == [1.5]


def test_server_error_retries_once_when_the_outage_persists(sleep_log):
    matcher, fake = make_matcher(http_error(503, "Service Unavailable"))
    response = ask(matcher)

    assert response.degraded is True and len(fake.calls) == 2
    assert sleep_log == [1.5]


def test_unclassified_transport_exception_still_degrades_the_verdict():
    """Anything the classifier has never seen cannot break a scan either."""
    matcher, fake = make_matcher(ZeroDivisionError("division by zero"))

    response = ask(matcher)

    assert response.degraded is True and response.error
    assert response.raw_data["payload"] == {}
    assert len(fake.calls) == 1


# --- 6. rate limit ------------------------------------------------------------


def test_rate_limit_is_typed_and_keeps_the_retry_after_hint():
    error = classify_api_error(http_error(429, "Too Many Requests",
                                         headers={"retry-after": "3"},
                                         error_class=RateLimitError))

    assert isinstance(error, AIRateLimitError)
    assert error.retry_after == 3.0
    # a absurd hint is respected but never parks a scan for minutes
    greedy = classify_api_error(http_error(429, "rate limit reached",
                                          headers={"retry-after": "999"},
                                          error_class=RateLimitError))
    assert greedy.retry_after == 30.0


def test_rate_limit_waits_for_retry_after_then_raises(sleep_log):
    matcher, fake = make_matcher(http_error(429, "Too Many Requests",
                                           headers={"retry-after": "3"},
                                           error_class=RateLimitError))

    response = ask(matcher)

    assert response.degraded is True and "rate limit" in response.error.lower()
    assert len(fake.calls) == 2 and sleep_log == [3.0]


def test_rate_limit_that_clears_on_the_second_attempt_is_not_a_failure(sleep_log):
    matcher, fake = make_matcher(
        http_error(429, "Too Many Requests", headers={"retry-after": "7"},
                   error_class=RateLimitError),
        completion(VALID_JSON),
    )

    response = ask(matcher)

    assert response.degraded is False and response.match_score == 82
    assert len(fake.calls) == 2 and sleep_log == [7.0]


# --- 7. unavailable / misconfigured model ------------------------------------


@pytest.mark.parametrize(
    "status, message",
    [
        (404, "Error code: 404 - the model `meta/dead-model` is not a valid model"),
        (400, "Error code: 400 - model not found"),
        (404, "Error code: 404 - model has been deprecated"),
        (400, "Error code: 400 - model is not supported on this endpoint"),
    ],
)
def test_dead_model_name_fails_fast_and_tells_the_user_where_to_fix_it(status, message):
    typed = classify_api_error(http_error(status, message))

    assert isinstance(typed, AIModelUnavailableError)
    assert "NVIDIA_MODEL" in str(typed)          # names the knob to turn


def test_dead_model_name_is_not_retried_because_a_retry_cannot_help():
    matcher, fake = make_matcher(http_error(
        404, "the model `nvidia/retired-model` is not a valid model"))

    response = ask(matcher)

    assert response.degraded is True
    assert "unavailable" in response.error.lower()
    assert len(fake.calls) == 1                  # fail fast, keep the scan moving
    assert response.priority is Priority.P3      # never promoted on a broken model


def test_a_wrong_key_fails_fast_and_points_at_the_key(sleep_log):
    matcher, fake = make_matcher(http_error(401, "Invalid API key"))

    response = ask(matcher)

    assert isinstance(classify_api_error(http_error(401, "Invalid API key")), AIAuthError)
    assert response.degraded is True and "NVIDIA_API_KEY" in response.error
    assert len(fake.calls) == 1                  # auth errors are never retried
    assert sleep_log == []


# --- 8. missing fields --------------------------------------------------------


def test_only_the_required_fields_are_enough():
    matcher, _ = make_matcher(completion(json.dumps(
        {"match": True, "match_score": 71, "priority": "P2", "reason": "Solid fit."})))

    response = ask(matcher)

    assert response.degraded is False
    assert response.match_score == 71 and response.priority is Priority.P2
    assert response.matched_skills == [] and response.missing_skills == []
    # absent judgements stay None - they are never guessed into True
    assert response.experience_match is None
    assert response.location_match is None
    assert response.role_match is None


def test_an_empty_object_is_an_answer_not_a_crash():
    matcher, _ = make_matcher(completion("{}"))

    response = ask(matcher)

    assert response.degraded is False            # the model did answer, uselessly
    assert response.match_score == 0
    assert response.priority is Priority.P3
    assert response.relevant is False
    assert response.reason == "" and response.reasons == []


@pytest.mark.parametrize("content", ["null", "[]", "\"P1\"", "3"])
def test_json_that_is_not_an_object_never_reaches_the_verdict(content, sleep_log):
    matcher, _ = make_matcher(completion(content))

    assert ask(matcher).degraded is True


def test_a_bare_true_answer_is_refused_rather_than_guessed():
    """`true` is valid JSON but has no verdict in it - degrade, do not invent one."""
    matcher, _ = make_matcher(completion("true"))

    assert ask(matcher).degraded is True


# --- 9. invalid score ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (900, 100),          # out of range high -> clamped
        (-5, 0),             # out of range low -> clamped
        ("85", 85),          # numeric string
        ("85%", 85),         # unit the model likes to append
        ("eighty", 0),       # no digits at all
        (0.87, 87),          # 0-1 scale, scaled back up
        (True, 0),           # a boolean is not a score
        (None, 0),
        ([], 0),
    ],
)
def test_any_shape_of_score_arrives_inside_0_and_100(raw, expected):
    payload = dict(VALID_PAYLOAD, match_score=raw)
    matcher, _ = make_matcher(completion(json.dumps(payload)))

    response = ask(matcher)

    assert response.degraded is False            # the answer is still usable
    assert 0 <= response.match_score <= 100
    assert response.match_score == expected

    verdict = combine(analyze(make_job(), make_profile()), make_config(), response)
    assert 0 <= verdict.match_score <= 100       # and so is the blended verdict


# --- 10. invalid priority -----------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("P2", Priority.P2),
        ("high", Priority.P1),                   # common alias
        ("medium", Priority.P2),
        ("P9", Priority.P3),                     # tier that does not exist
        ("urgent-ish", Priority.P3),
        ("", Priority.P3),
        (None, Priority.P3),
        (5, Priority.P3),                        # a number is not a tier
    ],
)
def test_any_shape_of_priority_arrives_as_a_real_tier(raw, expected):
    payload = dict(VALID_PAYLOAD, priority=raw)
    matcher, _ = make_matcher(completion(json.dumps(payload)))

    response = ask(matcher)

    assert response.priority is expected
    assert isinstance(response.priority, Priority)


# --- 11. NVIDIA_MODEL configuration -------------------------------------------


def test_nvidia_model_env_var_reaches_the_request(monkeypatch):
    monkeypatch.setenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v3.2")
    config = load_config()
    matcher, fake = make_matcher(completion(VALID_JSON), config=config)

    assert config.nvidia_model == "deepseek-ai/deepseek-v3.2"
    assert matcher.model == "deepseek-ai/deepseek-v3.2"

    ask(matcher)
    assert fake.calls[0]["model"] == "deepseek-ai/deepseek-v3.2"


def test_client_is_wired_from_the_config_never_a_hardcoded_model():
    """The endpoint, model and timeout all come from config, nothing else.

    (load_config() always re-reads .env, so the built-in default model is only
    visible on the dataclass - an explicit NVIDIA_MODEL wins over .env, which is
    covered by test_nvidia_model_env_var_reaches_the_request.)
    """
    assert Config().nvidia_model == "meta/llama-3.1-70b-instruct"   # documented fallback

    from_config = load_config()
    assert from_config.nvidia_model.strip()
    assert NVIDIAClient(from_config, API_KEY).model == from_config.nvidia_model

    client = NVIDIAClient(make_config(nvidia_base_url="https://example.test/v1",
                                      ai_timeout_seconds=11), API_KEY)
    assert client.model == "nvidia/test-model"
    assert str(client.client.base_url).startswith("https://example.test/v1")
    assert client.client.timeout == 11               # AI_TIMEOUT_SECONDS is honoured
    assert client.client.max_retries == 0            # our backoff owns retries


# --- 11b. an endpoint that rejects structured output -------------------------


def test_an_endpoint_that_rejects_structured_output_falls_back_to_prompted_json():
    """Model swaps happen: the client must still return a verdict, not an error.

    The retry must drop `response_format` and move the schema into the prompt -
    sending the same rejected payload again would just fail twice.
    """
    rejected = http_error(400, "Error code: 400 - Invalid request: 'response_format' is not valid for this model")
    matcher, fake = make_matcher(rejected, completion("```json\n" + VALID_JSON + "\n```"))

    response = ask(matcher)

    assert response.degraded is False and response.match_score == 82
    assert len(fake.calls) == 2
    assert "response_format" in fake.calls[0] and "response_format" not in fake.calls[1]
    assert fake.calls[1]["messages"][1]["content"].startswith("JSON schema:")
    assert fake.calls[1]["messages"][-1]["content"].endswith("No markdown, no commentary.")


def test_the_prompted_json_fallback_cannot_hide_a_second_bad_answer():
    rejected = http_error(400, "Invalid request: 'response_format' is not valid for this model")
    matcher, fake = make_matcher(rejected, completion("sorry, I cannot help with that"))

    assert ask(matcher).degraded is True
    assert len(fake.calls) == 2                    # one fallback, not an endless loop


def test_swapping_the_model_changes_the_error_text_too():
    """A retired model must be reported against the model actually configured."""
    matcher, _ = make_matcher(
        http_error(404, "model not found"), config=make_config(nvidia_model="nvidia/x"))

    response = ask(matcher)

    assert "nvidia/x" in response.raw_data["model"]
    assert "NVIDIA_MODEL" in response.error         # actionable, not "AI failed"


# --- 12. the API key must never leak -----------------------------------------


def test_api_key_never_appears_in_logs_errors_or_stored_payload(caplog, sleep_log):
    """Every failure mode is exercised with a canary key; none may repeat it."""
    request = httpx.Request("POST", BASE_URL + "/chat/completions")
    failures = [
        http_error(401, "Invalid API key"),
        http_error(403, "Forbidden"),
        http_error(429, "Too Many Requests", error_class=RateLimitError),
        http_error(404, "model not found"),
        http_error(500, "Internal server error"),
        APITimeoutError(request=request),
        completion("not json"),
        completion(""),
    ]
    for outcome in failures:
        matcher, _ = make_matcher(outcome)
        response = ask(matcher)
        assert response.degraded is True            # each one really failed
        assert API_KEY not in (response.error or "")
        assert API_KEY not in json.dumps(response.raw_data)
        assert API_KEY not in " | ".join(response.reasons)

    # ... and none of it reached the log file, at any level.
    assert API_KEY not in caplog.text
    assert "Authorization" not in caplog.text


def test_the_key_is_never_part_of_the_prompt_or_headers_sent_to_the_model():
    matcher, fake = make_matcher(completion(VALID_JSON))

    ask(matcher)

    sent = fake.calls[0]
    assert API_KEY not in json.dumps(sent["messages"])
    # the SDK owns auth; the matcher must not be smuggling the key into the body
    assert not any(API_KEY in str(value) for value in sent.values())


def test_prompt_builder_output_carries_no_credential():
    profile, job = make_profile(), make_job()
    prompt = build_user_prompt(job, profile, analyze(job, profile))

    assert API_KEY not in prompt
    assert "api key" not in prompt.lower()


# --- 13. the deterministic evidence reaches the model -------------------------


def test_deterministic_evidence_is_included_in_the_ai_request():
    job, profile = make_job(), make_profile()
    evidence = analyze(job, profile)

    prompt = build_user_prompt(job, profile, evidence)

    assert "DETERMINISTIC EVIDENCE" in prompt
    assert f"deterministic_score: {evidence.score}/100" in prompt
    assert "your_skills_mentioned_in_posting:" in prompt
    assert "your_skills_not_mentioned:" in prompt
    assert "skills_named_in_posting:" in prompt
    assert "work_mode:" in prompt
    assert "location_match: match" in prompt
    assert "location_score:" in prompt
    assert "transferable_evidence:" in prompt
    for skill in evidence.matched_skills[:2]:       # what the code already found
        assert skill in prompt


def test_the_model_is_told_the_evidence_is_ground_truth():
    assert "deterministic evidence" in SYSTEM_PROMPT.lower()
    assert "do not invent skills" in SYSTEM_PROMPT.lower()
    assert "do not count them a second time" in SYSTEM_PROMPT.lower()


def test_the_evidence_reaches_the_wire_not_just_the_prompt_builder():
    job, profile = make_job(), make_profile()
    evidence = analyze(job, profile)
    matcher, fake = make_matcher(completion(VALID_JSON))

    asyncio.run(matcher.match(job, profile, evidence))

    user_turn = fake.calls[0]["messages"][1]["content"]
    assert "DETERMINISTIC EVIDENCE" in user_turn
    assert f"deterministic_score: {evidence.score}/100" in user_turn


def test_no_evidence_means_no_evidence_block_rather_than_a_fake_one():
    job, profile = make_job(), make_profile()

    prompt = build_user_prompt(job, profile, None)

    assert "DETERMINISTIC EVIDENCE" not in prompt
    assert "CANDIDATE" in prompt and "JOB POSTING:" in prompt   # still a full ask


def test_the_prompt_states_the_candidate_and_the_posting_and_bounds_the_text():
    job = make_job(description="Python " * 2000)
    profile = make_profile()

    prompt = build_user_prompt(job, profile, analyze(job, profile))

    assert "Python Developer" in prompt                     # the posting
    assert "Backend Developer" in prompt                    # the profile's intent
    assert "Pune" in prompt
    assert "application_link_present: yes" in prompt
    assert len(prompt) < 6000                               # a runaway posting


# --- 14. the answer becomes a safe MatchResult ---------------------------------


def test_a_good_answer_blends_into_the_canonical_verdict():
    job, profile = make_job(), make_profile()
    matcher, _ = make_matcher(completion(VALID_JSON))

    response = ask(matcher)
    verdict = combine(analyze(job, profile), make_config(), response,
                      ai_model=matcher.model)

    assert isinstance(verdict, MatchResult)
    assert verdict.ai_status == "ok" and verdict.ai_score == 82
    assert verdict.ai_model == "nvidia/test-model"
    assert verdict.match_score == pytest.approx(
        round(verdict.deterministic_score * 0.6 + 82 * 0.4), abs=1)
    assert 0 <= verdict.match_score <= 100
    assert verdict.signals["ai"]["status"] == "ok"
    assert not verdict.degraded


def test_nonsense_values_cannot_produce_an_invalid_match_result():
    payload = {
        "match": "yes definitely",
        "match_score": "very high",
        "priority": "P9",
        "reason": None,
        "matched_skills": "Kubernetes; Rust",     # string, and hallucinated
        "missing_skills": [{"skill": "Go"}],
        "experience_match": "maybe",
    }
    matcher, _ = make_matcher(completion(json.dumps(payload)))

    response = ask(matcher)
    verdict = combine(analyze(make_job(), make_profile()), make_config(), response)

    assert isinstance(verdict.priority, Priority)
    assert 0 <= verdict.match_score <= 100
    # the model cannot claim a skill the posting never mentions
    assert "Kubernetes" not in verdict.matched_skills
    assert "Python" in verdict.matched_skills
    assert verdict.ai_status == "ok"


def test_a_failed_model_still_produces_a_usable_deterministic_verdict(sleep_log):
    job, profile = make_job(), make_profile()
    matcher, _ = make_matcher(http_error(429, "Too Many Requests",
                                         error_class=RateLimitError))

    response = ask(matcher)
    verdict = combine(analyze(job, profile), make_config(), response,
                      ai_model=matcher.model)

    assert verdict.ai_status == "failed" and verdict.degraded
    assert verdict.ai_score is None and verdict.ai_error
    assert verdict.match_score <= make_config().ai_failed_score_cap
    assert verdict.priority is not Priority.P1        # never the top banner on AI-down
    assert "AI unavailable" in verdict.reason
    assert verdict.to_ai_response().match_score == verdict.match_score


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
