"""NVIDIA/OpenAI-compatible AI client"""

import asyncio
import json
import logging
import re
from typing import Optional

from openai import AsyncOpenAI

from hirevia.config import Config

logger = logging.getLogger(__name__)


class AIError(RuntimeError):
    """Base class for AI failures; message is safe to log/store."""


class AIAuthError(AIError):
    """API key missing/invalid - retrying is pointless."""


class AIModelUnavailableError(AIError):
    """The configured model name is unknown, retired or not enabled."""


class AIRateLimitError(AIError):
    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class AITimeoutError(AIError):
    """Request timed out / connection dropped - worth one retry."""


class AIServerError(AIError):
    """The server failed temporarily - worth one retry."""


class AIInvalidResponseError(AIError):
    """The model answered, but not with usable JSON."""


class AIResponseFormatError(AIError):
    """The provider does not support the requested structured response format."""


# Phrases an OpenAI-compatible gateway uses when the model name is dead.
_MODEL_UNAVAILABLE_HINTS = (
    "is not a valid model", "model not found", "model does not exist",
    "model_not_found", "no such model", "not supported", "unsupported model",
    "not available", "no longer available", "has been deprecated",
    "is deprecated", "unavailable",
)


def classify_api_error(
    error: Exception,
    provider_name: str = "nvidia",
    api_key_env: str = "NVIDIA_API_KEY",
    model_env: str = "NVIDIA_MODEL",
) -> AIError:
    """Map SDK/transport errors to safe typed errors without echoing payloads."""
    from openai import APITimeoutError, APIConnectionError, RateLimitError

    status = getattr(error, "status_code", None)
    message = str(error) or type(error).__name__
    lowered = message.lower()

    if isinstance(error, (APITimeoutError, APIConnectionError)):
        return AITimeoutError(f"AI request did not complete (timed out; provider={provider_name})")
    if isinstance(error, RateLimitError) or status == 429 or "rate limit" in lowered:
        return AIRateLimitError(f"AI rate limit reached (provider={provider_name})", _retry_after(error))
    if status in (401, 403):
        return AIAuthError(
            f"AI authentication failed (HTTP {status}, provider={provider_name}); check {api_key_env}"
        )
    if "timeout" in lowered or "timed out" in lowered:
        return AITimeoutError(f"AI request did not complete (timed out; provider={provider_name})")
    if isinstance(status, int) and 500 <= status < 600:
        return AIServerError(f"AI server error (HTTP {status}, provider={provider_name})")
    if status == 400 and ("response_format" in lowered or "json_schema" in lowered):
        return AIResponseFormatError(
            f"provider={provider_name} does not support the configured structured response format"
        )
    if status == 404 or any(hint in lowered for hint in _MODEL_UNAVAILABLE_HINTS):
        return AIModelUnavailableError(
            f"configured AI model is unavailable (provider={provider_name}); set {model_env}"
        )
    return AIError(f"AI request failed (provider={provider_name}; error={type(error).__name__})")


def _retry_after(error: Exception) -> float:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    try:
        return min(float(headers.get("retry-after", 0) or 0), 30.0)
    except (TypeError, ValueError):
        return 0.0


def extract_json_object(content: str) -> dict:
    """Parse a JSON object out of a model response, tolerating markdown code
    fences and leading/trailing prose ("Here is the JSON: {...}")."""
    if not content or not content.strip():
        raise json.JSONDecodeError("empty response", content or "", 0)
    text = content.strip()

    # Strip ```json ... ``` fences
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise json.JSONDecodeError("not a JSON object", text, 0)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        raise


class OpenAICompatibleClient:
    # Models differ: some reject response_format=json_schema. On that failure
    # we transparently retry with prompt-instructed JSON so the pipeline keeps
    # working when NVIDIA_MODEL is swapped in .env.
    _JSON_INSTRUCTION = (
        "\n\nRespond with ONLY a valid JSON object matching the provided schema. "
        "No markdown, no commentary."
    )

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = 45,
        provider_name: str = "openai-compatible",
        api_key_env: str = "API_KEY",
        model_env: str = "MODEL",
    ):
        self.provider_name = provider_name
        self.api_key_env = api_key_env
        self.model_env = model_env
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,  # retries are handled here so backoff stays predictable
        )
        self.model = model

    async def chat_completion(
        self,
        messages: list[dict],
        temperature: float = 0.1,
        max_tokens: int = 500,
        response_format: Optional[dict] = None,
    ) -> str:
        """Get chat completion from the configured OpenAI-compatible endpoint.

        Raises a typed AIError subclass (never a raw SDK exception) so callers
        can distinguish 'bad model name' from 'try again later'.
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        try:
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            error = classify_api_error(e, self.provider_name, self.api_key_env, self.model_env)
            logger.debug(
                "[AI] request failed (provider=%s, error=%s, status=%s)",
                self.provider_name, type(e).__name__, getattr(e, "status_code", None),
            )
            raise error from e

        message = response.choices[0].message if response.choices else None
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise AIInvalidResponseError(f"empty completion from model={self.model}")
        return content

    async def structured_completion(
        self,
        messages: list[dict],
        schema: dict,
        temperature: float = 0.1,
        max_tokens: int = 500,
        retries: int = 1,
    ) -> dict:
        """Get structured JSON completion with validation.

        Robust to model swaps: if the endpoint rejects response_format, we
        fall back to prompt-instructed JSON parsed with fence/prose tolerance.
        Rate limits and timeouts are retried with backoff; auth and unknown
        model errors surface immediately because retrying cannot help.
        """
        if (
            self.provider_name == "nvidia"
            and self.model.rsplit("/", 1)[-1].casefold() == "nemotron-3.5-lightning-30b-a3b"
        ):
            return await self._prompt_json_completion(messages, schema, temperature, max_tokens)

        response_format = {"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}

        last_error: Optional[Exception] = None
        content = ""
        attempts = max(1, retries + 1)
        for attempt in range(attempts):
            try:
                content = await self.chat_completion(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
                return extract_json_object(content)
            except json.JSONDecodeError as e:
                last_error = AIInvalidResponseError(f"model returned unusable JSON: {e}")
                logger.debug("[AI] invalid JSON (attempt %d/%d)", attempt + 1, attempts)
                if attempt < attempts - 1:
                    messages = messages + [
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": "The previous response was invalid JSON. Respond with ONLY valid JSON matching the schema."},
                    ]
                    continue
            except (AIModelUnavailableError, AIAuthError):
                raise  # a different attempt would fail identically
            except AIRateLimitError as e:
                last_error = e
                if attempt < attempts - 1:
                    await asyncio.sleep(max(e.retry_after, 2.0 * (attempt + 1)))
                    continue
                raise
            except (AITimeoutError, AIServerError) as e:
                last_error = e
                if attempt < attempts - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
            except AIResponseFormatError:
                return await self._prompt_json_completion(messages, schema, temperature, max_tokens)
            except AIError as e:
                raise

        raise last_error or AIInvalidResponseError("AI completion failed")

    async def _prompt_json_completion(
        self,
        messages: list[dict],
        schema: dict,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        """Fallback path for models without structured-output support."""
        schema_hint = json.dumps(schema, ensure_ascii=False)[:1500]
        is_nemotron = (
            self.provider_name == "nvidia"
            and self.model.rsplit("/", 1)[-1].casefold() == "nemotron-3.5-lightning-30b-a3b"
        )
        if is_nemotron:
            task = "\n\n".join(
                str(message.get("content", ""))
                for message in messages
                if message.get("content")
            )
            prompt = [{
                "role": "user",
                "content": "JSON schema:\n" + schema_hint + "\n\n" + task + self._JSON_INSTRUCTION,
            }]
        else:
            prompt = [dict(messages[0])] if messages and messages[0].get("role") == "system" else []
            rest = messages[len(prompt):]
            prompt.append({"role": "user", "content": "JSON schema:\n" + schema_hint})
            prompt.extend(rest)
            prompt[-1] = {
                **prompt[-1],
                "content": prompt[-1].get("content", "") + self._JSON_INSTRUCTION,
            }

        content = await self.chat_completion(
            messages=prompt, temperature=temperature, max_tokens=max_tokens, response_format=None
        )
        try:
            return extract_json_object(content)
        except json.JSONDecodeError as error:
            raise AIInvalidResponseError(
                f"model returned unusable JSON: {error.msg}"
            ) from error


class NVIDIAClient(OpenAICompatibleClient):
    """Backward-compatible NVIDIA endpoint client."""

    def __init__(self, config: Config, api_key: str):
        super().__init__(
            base_url=config.nvidia_base_url,
            model=config.nvidia_model,
            api_key=api_key,
            timeout=getattr(config, "ai_timeout_seconds", 45),
            provider_name="nvidia",
            api_key_env="NVIDIA_API_KEY",
            model_env="NVIDIA_MODEL",
        )
