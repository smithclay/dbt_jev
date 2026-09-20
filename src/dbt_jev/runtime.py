"""Validated, synchronous Jev Choice calls shared by DuckDB and ClickHouse."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient

QUESTION_ID = "classification"
QUESTION_INSTRUCTIONS = "Classify the state into exactly one of the supplied choices."
MAX_CHOICES = 255
PROVIDERS = {"typesafe", "openrouter"}
OPENROUTER_RETRY_STATUSES = {429, 500, 502, 503, 504, 524, 529}


class JevError(RuntimeError):
    """A safe-to-display error from the dbt_jev runtime."""


class JevCriteriaError(JevError, ValueError):
    """The supplied compile-time criteria are invalid."""


@dataclass(frozen=True)
class RuntimeConfig:
    """Non-secret runtime settings. The API key is read separately from the environment."""

    provider: str = "typesafe"
    base_url: str = "https://api.typesafe.ai"
    model: str = "jev-latest"
    request_timeout: float = 10.0
    max_retries: int = 2
    backoff_initial: float = 0.5
    backoff_max: float = 5.0
    retry_budget: float = 30.0

    @classmethod
    def from_env(cls, overrides: Mapping[str, Any] | None = None) -> RuntimeConfig:
        provider = str(
            (overrides or {}).get("provider")
            or os.getenv("DBT_JEV_PROVIDER", cls.provider)
        ).lower()
        if provider == "openrouter":
            default_base_url = "https://openrouter.ai"
            default_model = "typesafe/jev-1.13"
            provider_base_url = os.getenv("OPENROUTER_BASE_URL", default_base_url)
            provider_model = os.getenv("OPENROUTER_MODEL", default_model)
        else:
            provider_base_url = os.getenv("TYPESAFE_BASE_URL", cls.base_url)
            provider_model = os.getenv("TYPESAFE_DEFAULT_MODEL", cls.model)

        values: dict[str, Any] = {
            "provider": provider,
            "base_url": os.getenv("DBT_JEV_BASE_URL", provider_base_url),
            "model": os.getenv("DBT_JEV_MODEL", provider_model),
            "request_timeout": os.getenv(
                "DBT_JEV_REQUEST_TIMEOUT", cls.request_timeout
            ),
            "max_retries": os.getenv("DBT_JEV_MAX_RETRIES", cls.max_retries),
            "backoff_initial": os.getenv(
                "DBT_JEV_BACKOFF_INITIAL", cls.backoff_initial
            ),
            "backoff_max": os.getenv("DBT_JEV_BACKOFF_MAX", cls.backoff_max),
            "retry_budget": os.getenv("DBT_JEV_RETRY_BUDGET", cls.retry_budget),
        }
        if overrides:
            unknown = set(overrides) - set(values)
            if unknown:
                raise JevError(
                    f"Unknown dbt_jev runtime setting(s): {', '.join(sorted(unknown))}"
                )
            values.update(
                {key: value for key, value in overrides.items() if value is not None}
            )

        try:
            config = cls(
                provider=str(values["provider"]).lower(),
                base_url=str(values["base_url"]).rstrip("/"),
                model=str(values["model"]),
                request_timeout=float(values["request_timeout"]),
                max_retries=int(values["max_retries"]),
                backoff_initial=float(values["backoff_initial"]),
                backoff_max=float(values["backoff_max"]),
                retry_budget=float(values["retry_budget"]),
            )
        except (TypeError, ValueError):
            raise JevError("Invalid numeric dbt_jev runtime setting") from None
        config.validate()
        return config

    def validate(self) -> None:
        if self.provider not in PROVIDERS:
            raise JevError(
                f"Unsupported DBT_JEV_PROVIDER {self.provider!r}; expected typesafe or openrouter"
            )
        if not self.base_url or not self.model:
            raise JevError("Jev base URL and model must be non-empty")
        if self.request_timeout <= 0 or self.retry_budget <= 0:
            raise JevError("Jev timeouts must be greater than zero")
        if self.max_retries < 0:
            raise JevError("DBT_JEV_MAX_RETRIES must be zero or greater")
        if self.backoff_initial < 0 or self.backoff_max < 0:
            raise JevError("Jev retry backoff values must be zero or greater")


def validate_choices(value: str | Mapping[str, Any]) -> dict[str, str]:
    """Return a plain validated Choice criteria mapping without making a request."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise JevCriteriaError("Jev choices must be valid JSON") from exc
    else:
        decoded = value

    if not isinstance(decoded, Mapping):
        raise JevCriteriaError("Jev choices must be a JSON object")
    if len(decoded) < 2:
        raise JevCriteriaError("Jev Choice requires at least two labels")
    if len(decoded) > MAX_CHOICES:
        raise JevCriteriaError(f"Jev Choice supports at most {MAX_CHOICES} labels")

    choices: dict[str, str] = {}
    for label, description in decoded.items():
        if not isinstance(label, str) or not label:
            raise JevCriteriaError("Every Jev choice label must be a non-empty string")
        if not isinstance(description, str) or not description:
            raise JevCriteriaError(
                "Every Jev choice description must be a non-empty string"
            )
        choices[label] = description
    return choices


class JevClassifier:
    """Long-lived scalar classifier using TypeSafe directly or OpenRouter Decisions."""

    def __init__(
        self, config: RuntimeConfig | None = None, *, api_key: str | None = None
    ):
        self.config = config or RuntimeConfig.from_env()
        self.config.validate()
        credential_env = (
            "OPENROUTER_API_KEY"
            if self.config.provider == "openrouter"
            else "TYPESAFE_API_KEY"
        )
        resolved_api_key = api_key if api_key is not None else os.getenv(credential_env)
        if not resolved_api_key:
            raise JevError(
                f"Jev authentication is not configured; set {credential_env}"
            )

        self._api_key = resolved_api_key
        self._client: TypeSafeClient | None = None
        if self.config.provider == "typesafe":
            retry = RetryPolicy(
                max_retries=self.config.max_retries,
                backoff_initial=self.config.backoff_initial,
                backoff_max=self.config.backoff_max,
                backoff_jitter=0.0,
                timeout=self.config.retry_budget,
            )
            try:
                self._client = TypeSafeClient(
                    api_key=resolved_api_key,
                    base_url=self.config.base_url,
                    model=self.config.model,
                    timeout=self.config.request_timeout,
                    retry=retry,
                )
            except Exception:  # noqa: BLE001 - convert SDK configuration failures safely
                raise JevError(
                    "Failed to initialise the Jev client; check runtime configuration"
                ) from None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, overrides: Mapping[str, Any] | None = None) -> JevClassifier:
        return cls(RuntimeConfig.from_env(overrides))

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def classify(
        self, state: str | None, choices: str | Mapping[str, Any]
    ) -> str | None:
        if state is None:
            return None
        criteria = validate_choices(choices)
        try:
            with self._lock:
                if self.config.provider == "openrouter":
                    label = self._classify_openrouter(str(state), criteria)
                else:
                    # The official TypeSafe SDK owns connection reuse and bounded
                    # transient retries. Do not assume its sync client is thread-safe.
                    question = Choice(
                        instructions=QUESTION_INSTRUCTIONS, criteria=criteria
                    )
                    assert self._client is not None
                    response = self._client.system_one(
                        state=str(state),
                        questions={QUESTION_ID: question},
                    )
                    answer = response.answers[QUESTION_ID]
                    label = answer.choice
        except Exception as exc:
            if isinstance(exc, JevError):
                raise
            raise _sanitised_error(exc, self.config) from None

        if not isinstance(label, str):
            raise JevError(
                "Jev returned a malformed Choice response: choice is not text"
            )
        if label not in criteria:
            raise JevError("Jev returned a Choice label outside the supplied criteria")
        return label

    def _classify_openrouter(self, state: str, criteria: dict[str, str]) -> Any:
        payload = json.dumps(
            {
                "model": self.config.model,
                "state": state,
                "questions": {
                    QUESTION_ID: {
                        "type": "choice",
                        "instructions": QUESTION_INSTRUCTIONS,
                        "criteria": criteria,
                    }
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        url = f"{self.config.base_url}/api/alpha/decisions"
        started = time.monotonic()
        attempts = self.config.max_retries + 1

        for attempt in range(attempts):
            remaining = self.config.retry_budget - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            request = urllib.request.Request(
                url,
                data=payload,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=min(self.config.request_timeout, remaining)
                ) as response:
                    raw = response.read()
                try:
                    decoded = json.loads(raw)
                    answer = decoded["answers"][QUESTION_ID]
                    if answer.get("type") != "choice":
                        raise KeyError("type")
                    return answer["choice"]
                except (AttributeError, KeyError, TypeError, json.JSONDecodeError):
                    raise JevError("Jev returned a malformed response") from None
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if status not in OPENROUTER_RETRY_STATUSES or attempt + 1 >= attempts:
                    raise _HTTPFailure(status) from None
            except TimeoutError as exc:
                if attempt + 1 >= attempts:
                    raise TimeoutError from exc
            except urllib.error.URLError as exc:
                if attempt + 1 >= attempts:
                    if isinstance(exc.reason, TimeoutError):
                        raise TimeoutError from exc
                    raise _ConnectionFailure() from None

            delay = min(
                self.config.backoff_initial * (2**attempt),
                self.config.backoff_max,
            )
            remaining = self.config.retry_budget - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            if delay:
                time.sleep(min(delay, remaining))

        raise AssertionError("unreachable")


class _HTTPFailure(Exception):
    def __init__(self, status: int):
        self.status = status


class _ConnectionFailure(Exception):
    pass


def _sanitised_error(exc: Exception, config: RuntimeConfig) -> JevError:
    status = getattr(exc, "status", None)
    name = type(exc).__name__.lower()
    if status == 401 or "authentication" in name:
        credential = (
            "OPENROUTER_API_KEY"
            if config.provider == "openrouter"
            else "TYPESAFE_API_KEY"
        )
        return JevError(f"Jev authentication failed (HTTP 401); check {credential}")
    if "responsevalidation" in name or "validation" in name:
        return JevError("Jev returned a malformed response")
    if "timeout" in name or isinstance(exc, TimeoutError):
        return JevError(
            f"Jev request timed out after bounded retries (per-attempt timeout {config.request_timeout:g}s)"
        )
    if status is not None:
        return JevError(
            f"Jev request failed with HTTP {status} after at most {config.max_retries + 1} attempts"
        )
    return JevError(
        f"Jev request failed after at most {config.max_retries + 1} attempts"
    )


_default_classifier: JevClassifier | None = None
_default_lock = threading.Lock()


def classify(state: str | None, choices: str | Mapping[str, Any]) -> str | None:
    """Convenience entry point for runtimes that retain module globals."""

    global _default_classifier
    if state is None:
        return None
    # Validate before initialising the client so bad criteria cannot be hidden by a
    # missing credential and can never result in an HTTP request.
    criteria = validate_choices(choices)
    if _default_classifier is None:
        with _default_lock:
            if _default_classifier is None:
                _default_classifier = JevClassifier.from_env()
    return _default_classifier.classify(state, criteria)
