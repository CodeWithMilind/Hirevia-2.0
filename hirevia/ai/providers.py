"""Provider routing for OpenAI-compatible AI endpoints."""

import logging
import os
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol
from urllib.parse import urlparse

from hirevia.ai.client import (
    AIAuthError,
    AIError,
    AIInvalidResponseError,
    AIModelUnavailableError,
    AIRateLimitError,
    AIServerError,
    AITimeoutError,
    OpenAICompatibleClient,
)
from hirevia.config import Config

logger = logging.getLogger(__name__)


class AIProvider(Protocol):
    model: str

    async def structured_completion(self, **kwargs) -> dict:
        """Return one parsed structured response or raise a typed AIError."""


class AIConfigurationError(AIError):
    """No usable provider is configured."""


class AIProviderRouter:
    """Try providers in configured order and keep transient failures in cooldown."""

    _FALLBACK_ERRORS = (
        AITimeoutError,
        AIRateLimitError,
        AIServerError,
        AIModelUnavailableError,
        AIInvalidResponseError,
    )

    def __init__(
        self,
        providers: Mapping[str, AIProvider],
        primary: str,
        fallback: List[str],
        cooldown_seconds: int = 120,
        clock: Callable[[], float] = time.monotonic,
        configuration_errors: Optional[Mapping[str, str]] = None,
    ):
        self.providers = dict(providers)
        self.configuration_errors = dict(configuration_errors or {})
        self.primary = primary
        self.fallback = [name for name in fallback if name != primary]
        self.cooldown_seconds = max(1, min(int(cooldown_seconds), 300))
        self._clock = clock
        self._cooldown_until: Dict[str, float] = {}
        self.last_provider: Optional[str] = None
        self.last_model: Optional[str] = None
        self._warned_cooldown: set[str] = set()

    @property
    def provider_name(self) -> str:
        if self.last_provider:
            return self.last_provider
        if self.primary in self.providers:
            return self.primary
        return next((name for name in self.fallback if name in self.providers), self.primary)

    @property
    def model(self) -> str:
        active = self.providers.get(self.primary)
        if active is not None:
            return active.model
        for name in self.fallback:
            provider = self.providers.get(name)
            if provider is not None:
                return provider.model
        return ""

    @property
    def client(self):
        """Compatibility bridge for existing tests/integrations wiring transports."""
        provider = self.providers.get(self.primary)
        if provider is None:
            provider = next(iter(self.providers.values()), None)
        return getattr(provider, "client", None)

    @client.setter
    def client(self, transport) -> None:
        provider = self.providers.get(self.primary)
        if provider is None:
            provider = next(iter(self.providers.values()), None)
        if provider is not None:
            setattr(provider, "client", transport)

    async def structured_completion(self, **kwargs) -> dict:
        order = [self.primary, *self.fallback]
        last_error: Optional[AIError] = None
        now = self._clock()
        for name in order:
            provider = self.providers.get(name)
            if provider is None:
                if name in self.configuration_errors:
                    self.last_provider = name
                    self.last_model = None
                    raise AIConfigurationError(
                        f"AI provider {name} has invalid endpoint configuration"
                    )
                logger.debug("[AI] provider %s is not configured; skipping", name)
                continue
            cooldown_until = self._cooldown_until.get(name, 0.0)
            if cooldown_until > now:
                if name not in self._warned_cooldown:
                    logger.warning("[AI] provider %s is in cooldown", name)
                    self._warned_cooldown.add(name)
                continue
            self._warned_cooldown.discard(name)
            self._cooldown_until.pop(name, None)
            self.last_provider = name
            self.last_model = provider.model
            try:
                result = await provider.structured_completion(**kwargs)
                self.last_provider = name
                self.last_model = provider.model
                return result
            except self._FALLBACK_ERRORS as error:
                last_error = error
                delay = max(self.cooldown_seconds, getattr(error, "retry_after", 0.0))
                self._cooldown_until[name] = now + min(delay, 300)
                if name not in self._warned_cooldown:
                    logger.warning(
                        "[AI] provider %s temporarily unavailable (%s); cooling down",
                        name, type(error).__name__,
                    )
                    self._warned_cooldown.add(name)
                else:
                    logger.debug("[AI] provider %s remains unavailable", name)
            except AIAuthError:
                raise
            except AIError:
                raise
            except Exception as error:  # defensive boundary for injected/third-party providers
                raise AIError(f"AI provider {name} failed ({type(error).__name__})") from error

        if last_error is not None:
            raise last_error
        names = ", ".join(order) or "none"
        raise AIConfigurationError(f"no configured AI providers available (tried: {names})")


def build_ai_provider_router(
    config: Config,
    primary_api_key: Optional[str] = None,
    primary_provider: Optional[AIProvider] = None,
) -> AIProviderRouter:
    """Build configured endpoints using API keys sourced only from environment variables."""
    settings = config.ai
    providers: Dict[str, AIProvider] = {}
    configuration_errors: Dict[str, str] = {}
    if not settings.enabled:
        return AIProviderRouter(
            providers, settings.primary, settings.fallback, settings.cooldown_seconds
        )
    for name in dict.fromkeys([settings.primary, *settings.fallback]):
        if name == settings.primary and primary_provider is not None:
            providers[name] = primary_provider
            continue
        spec = settings.providers.get(name)
        if spec is None or spec.type != "openai_compatible":
            continue
        key = primary_api_key if name == "nvidia" and primary_api_key else None
        if key is None and spec.api_key_env:
            key = os.getenv(spec.api_key_env)
        model = config.nvidia_model if name == "nvidia" else os.getenv(f"{name.upper()}_MODEL", spec.model)
        base_url = config.nvidia_base_url if name == "nvidia" else os.getenv(f"{name.upper()}_BASE_URL", spec.base_url)
        if not model or not base_url:
            continue
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or parsed_url.username:
            configuration_errors[name] = "invalid endpoint"
            continue
        if not key:
            continue
        try:
            providers[name] = OpenAICompatibleClient(
                base_url=base_url,
                model=model,
                api_key=key,
                timeout=settings.timeout_seconds,
                provider_name=name,
                api_key_env=spec.api_key_env or "API_KEY",
                model_env="NVIDIA_MODEL" if name == "nvidia" else f"{name.upper()}_MODEL",
            )
        except Exception:
            configuration_errors[name] = "invalid endpoint"
    return AIProviderRouter(
        providers,
        primary=settings.primary,
        fallback=settings.fallback,
        cooldown_seconds=settings.cooldown_seconds,
        configuration_errors=configuration_errors,
    )


def provider_status(config: Config) -> List[dict[str, Any]]:
    """Return configuration status only; this performs no network health checks."""
    rows = []
    for name, spec in config.ai.providers.items():
        model = config.nvidia_model if name == "nvidia" else os.getenv(f"{name.upper()}_MODEL", spec.model)
        base_url = config.nvidia_base_url if name == "nvidia" else os.getenv(f"{name.upper()}_BASE_URL", spec.base_url)
        key_ready = bool(spec.api_key_env and os.getenv(spec.api_key_env))
        parsed_url = urlparse(base_url) if base_url else None
        valid_url = bool(
            parsed_url
            and parsed_url.scheme in {"http", "https"}
            and parsed_url.netloc
            and not parsed_url.username
        )
        rows.append({
            "name": name,
            "model": model,
            "configured": bool(config.ai.enabled and model and valid_url and key_ready),
            "primary": name == config.ai.primary,
        })
    return rows
