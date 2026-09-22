"""Validated Jev calls shared by DuckDB and ClickHouse.

The runtime keeps a single scalar contract (`classify`, `match_probability`,
`score`, `decisions`) but is built for throughput:

- bounded concurrency (`DBT_JEV_MAX_CONCURRENCY`) so many rows can be in flight
  at once instead of one at a time;
- in-run de-duplication so identical inputs cost one request, not many;
- per-thread keep-alive transport so the OpenRouter route reuses connections;
- jittered, ``Retry-After``-aware backoff so concurrent retries do not
  synchronise into a rate-limit storm;
- ``decisions`` batches several typed questions against one state into a single
  request, which Jev scores in parallel for roughly the cost of one question.
"""

from __future__ import annotations

import datetime
import http.client
import json
import math
import os
import random
import re
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

from typesafe_sdk import Choice, Noul, RetryPolicy, Score, TypeSafeClient

CLASSIFICATION_QUESTION_ID = "classification"
MATCH_QUESTION_ID = "match"
SCORE_QUESTION_ID = "score"
QUESTION_INSTRUCTIONS = "Classify the state into exactly one of the supplied choices."
MAX_CHOICES = 255
MAX_QUESTIONS = 255
NOUL_CRITERIA_KEYS = {"true", "false"}
PROVIDERS = {"typesafe", "openrouter"}
OPENROUTER_RETRY_STATUSES = {429, 500, 502, 503, 504, 524, 529}
OPENROUTER_DECISIONS_PATH = "/api/alpha/decisions"
_QUESTION_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    backoff_jitter: float = 0.5
    retry_budget: float = 30.0
    max_concurrency: int = 8

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
            "backoff_jitter": os.getenv("DBT_JEV_BACKOFF_JITTER", cls.backoff_jitter),
            "retry_budget": os.getenv("DBT_JEV_RETRY_BUDGET", cls.retry_budget),
            "max_concurrency": os.getenv(
                "DBT_JEV_MAX_CONCURRENCY", cls.max_concurrency
            ),
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
                backoff_jitter=float(values["backoff_jitter"]),
                retry_budget=float(values["retry_budget"]),
                max_concurrency=int(values["max_concurrency"]),
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
        if not 0.0 <= self.backoff_jitter <= 1.0:
            raise JevError("DBT_JEV_BACKOFF_JITTER must be between 0 and 1")
        if self.max_concurrency < 1:
            raise JevError("DBT_JEV_MAX_CONCURRENCY must be one or greater")


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


def validate_instructions(value: Any, primitive: str) -> str:
    """Return non-empty instructions for a Noul or Score question."""

    if not isinstance(value, str) or not value:
        raise JevCriteriaError(f"Jev {primitive} instructions must be non-empty text")
    return value


def validate_noul_criteria(
    value: str | Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Return optional true/false descriptions for a Noul question."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise JevCriteriaError("Jev Noul criteria must be valid JSON") from exc
    else:
        decoded = value

    if decoded is None:
        return None
    if not isinstance(decoded, Mapping):
        raise JevCriteriaError("Jev Noul criteria must be a JSON object or null")
    unknown = set(decoded) - NOUL_CRITERIA_KEYS
    if unknown:
        raise JevCriteriaError(
            "Jev Noul criteria only supports the labels 'true' and 'false'"
        )

    criteria: dict[str, str] = {}
    for label, description in decoded.items():
        if not isinstance(description, str) or not description:
            raise JevCriteriaError(
                "Every Jev Noul criterion description must be non-empty text"
            )
        criteria[label] = description
    return criteria or None


def validate_levels(value: str | Sequence[Any]) -> list[str]:
    """Return a validated ordered Score rubric without making a request."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise JevCriteriaError("Jev Score levels must be valid JSON") from exc
    else:
        decoded = value

    if isinstance(decoded, (str, bytes)) or not isinstance(decoded, Sequence):
        raise JevCriteriaError("Jev Score levels must be a JSON array")
    if not decoded:
        raise JevCriteriaError("Jev Score requires at least one level")

    levels: list[str] = []
    for description in decoded:
        if not isinstance(description, str) or not description:
            raise JevCriteriaError(
                "Every Jev Score level must be a non-empty text description"
            )
        levels.append(description)
    return levels


def validate_questions(
    value: str | Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return a validated map of question id to normalised question metadata.

    Each question is a ``choice``, ``noul``, or ``score`` primitive evaluated
    against one shared state. This is the compile-time contract for the
    ``decisions`` macro and never makes a request.
    """

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise JevCriteriaError("Jev questions must be valid JSON") from exc
    else:
        decoded = value

    if not isinstance(decoded, Mapping):
        raise JevCriteriaError("Jev questions must be a JSON object")
    if not decoded:
        raise JevCriteriaError("Jev requires at least one question")
    if len(decoded) > MAX_QUESTIONS:
        raise JevCriteriaError(
            f"Jev supports at most {MAX_QUESTIONS} questions per request"
        )

    meta: dict[str, dict[str, Any]] = {}
    for question_id, spec in decoded.items():
        if not isinstance(question_id, str) or not _QUESTION_ID_RE.match(question_id):
            raise JevCriteriaError(
                "Every Jev question id must match [A-Za-z_][A-Za-z0-9_]*"
            )
        if not isinstance(spec, Mapping):
            raise JevCriteriaError("Every Jev question must be a JSON object")
        question_type = spec.get("type")
        if question_type == "choice":
            normalised: dict[str, Any] = {
                "type": "choice",
                "choices": validate_choices(spec.get("choices")),
            }
            instructions = spec.get("instructions")
            if instructions is not None:
                normalised["instructions"] = validate_instructions(
                    instructions, "Choice"
                )
        elif question_type == "noul":
            normalised = {
                "type": "noul",
                "instructions": validate_instructions(spec.get("instructions"), "Noul"),
                "criteria": validate_noul_criteria(spec.get("criteria")),
            }
        elif question_type == "score":
            normalised = {
                "type": "score",
                "levels": validate_levels(spec.get("levels")),
                "instructions": validate_instructions(
                    spec.get("instructions"), "Score"
                ),
            }
        else:
            raise JevCriteriaError(
                "Every Jev question type must be 'choice', 'noul', or 'score'"
            )
        meta[question_id] = normalised
    return meta


class _OpenRouterConn:
    """A single reusable keep-alive HTTP(S) connection for the OpenRouter route.

    One instance lives per worker thread, so connections are reused across rows
    without sharing a socket between threads.
    """

    def __init__(self, base_url: str):
        parts = urlsplit(base_url)
        self._https = parts.scheme != "http"
        self._host = parts.hostname or ""
        self._port = parts.port
        self._path_prefix = parts.path.rstrip("/")
        self._conn: http.client.HTTPConnection | None = None

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        if self._https:
            return http.client.HTTPSConnection(self._host, self._port, timeout=timeout)
        return http.client.HTTPConnection(self._host, self._port, timeout=timeout)

    def post(
        self,
        subpath: str,
        payload: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes, float | None]:
        if self._conn is None:
            self._conn = self._connect(timeout)
        else:
            # Reuse the open socket but honour the per-attempt timeout budget.
            self._conn.timeout = timeout
            if self._conn.sock is not None:
                self._conn.sock.settimeout(timeout)
        try:
            self._conn.request(
                "POST", self._path_prefix + subpath, body=payload, headers=headers
            )
            response = self._conn.getresponse()
            body = response.read()
            status = response.status
            retry_after = _parse_retry_after(response.getheader("Retry-After"))
            if response.will_close:
                self.close()
            return status, body, retry_after
        except (http.client.HTTPException, OSError) as exc:
            self.close()
            if isinstance(exc, TimeoutError):
                raise TimeoutError from exc
            raise _ConnectionFailure() from None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - best-effort socket teardown
                pass
            self._conn = None


class JevClassifier:
    """Long-lived scalar decision client for TypeSafe or OpenRouter Decisions.

    Provider clients and OpenRouter connections are held per worker thread, so a
    bounded pool of rows can be evaluated concurrently without sharing a
    non-thread-safe client or socket.
    """

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
        self._local = threading.local()
        self._registry_lock = threading.Lock()
        self._typesafe_clients: list[TypeSafeClient] = []
        self._openrouter_conns: list[_OpenRouterConn] = []
        self._executor: ThreadPoolExecutor | None = None

    @classmethod
    def from_env(cls, overrides: Mapping[str, Any] | None = None) -> JevClassifier:
        return cls(RuntimeConfig.from_env(overrides))

    def close(self) -> None:
        executor = self._executor
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        with self._registry_lock:
            clients = list(self._typesafe_clients)
            conns = list(self._openrouter_conns)
        for client in clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        for conn in conns:
            conn.close()

    # -- single-row entry points -------------------------------------------------

    def classify(
        self, state: str | None, choices: str | Mapping[str, Any]
    ) -> str | None:
        if state is None:
            return None
        criteria = validate_choices(choices)
        return self._classify_one(str(state), criteria)

    def match_probability(
        self,
        left: str | None,
        right: str | None,
        instructions: str,
        criteria: str | Mapping[str, Any] | None = None,
    ) -> float | None:
        """Return the probability that two values match, or ``None`` for NULL input."""

        if left is None or right is None:
            return None
        instructions = validate_instructions(instructions, "Noul")
        criteria = validate_noul_criteria(criteria)
        return self._match_one(str(left), str(right), instructions, criteria)

    def score(
        self,
        state: str | None,
        levels: str | Sequence[Any],
        instructions: str,
    ) -> float | None:
        """Return the expected score for an ordered rubric, or ``None`` for NULL."""

        if state is None:
            return None
        levels = validate_levels(levels)
        instructions = validate_instructions(instructions, "Score")
        return self._score_one(str(state), levels, instructions)

    def decisions(
        self, state: str | None, questions: str | Mapping[str, Any]
    ) -> str | None:
        """Answer several questions against one state in a single request.

        Returns a compact JSON object mapping each question id to its scalar
        answer, or ``None`` for NULL input.
        """

        if state is None:
            return None
        meta = validate_questions(questions)
        return self._decisions_one(str(state), meta)

    # -- batch entry points ------------------------------------------------------

    def classify_many(
        self, states: Sequence[str | None], choices: str | Mapping[str, Any]
    ) -> list[str | None]:
        criteria = validate_choices(choices)
        return self._batch(
            list(states),
            is_null=lambda state: state is None,
            key=lambda state: str(state),
            work=lambda state: self._classify_one(str(state), criteria),
        )

    def match_probability_many(
        self,
        lefts: Sequence[str | None],
        rights: Sequence[str | None],
        instructions: str,
        criteria: str | Mapping[str, Any] | None = None,
    ) -> list[float | None]:
        instructions = validate_instructions(instructions, "Noul")
        criteria = validate_noul_criteria(criteria)
        pairs = list(zip(lefts, rights))
        return self._batch(
            pairs,
            is_null=lambda pair: pair[0] is None or pair[1] is None,
            key=lambda pair: (str(pair[0]), str(pair[1])),
            work=lambda pair: self._match_one(
                str(pair[0]), str(pair[1]), instructions, criteria
            ),
        )

    def score_many(
        self,
        states: Sequence[str | None],
        levels: str | Sequence[Any],
        instructions: str,
    ) -> list[float | None]:
        levels = validate_levels(levels)
        instructions = validate_instructions(instructions, "Score")
        return self._batch(
            list(states),
            is_null=lambda state: state is None,
            key=lambda state: str(state),
            work=lambda state: self._score_one(str(state), levels, instructions),
        )

    def decisions_many(
        self, states: Sequence[str | None], questions: str | Mapping[str, Any]
    ) -> list[str | None]:
        meta = validate_questions(questions)
        return self._batch(
            list(states),
            is_null=lambda state: state is None,
            key=lambda state: str(state),
            work=lambda state: self._decisions_one(str(state), meta),
        )

    # -- request construction ----------------------------------------------------

    def _classify_one(self, state: str, criteria: dict[str, str]) -> str:
        answers = self._ask_questions(
            state,
            {
                CLASSIFICATION_QUESTION_ID: Choice(
                    instructions=QUESTION_INSTRUCTIONS, criteria=criteria
                )
            },
            {
                CLASSIFICATION_QUESTION_ID: {
                    "type": "choice",
                    "instructions": QUESTION_INSTRUCTIONS,
                    "criteria": criteria,
                }
            },
        )
        return _extract_choice(
            _require_answer(answers, CLASSIFICATION_QUESTION_ID), criteria
        )

    def _match_one(
        self,
        left: str,
        right: str,
        instructions: str,
        criteria: dict[str, str] | None,
    ) -> float:
        state = {"left": left, "right": right}
        wire: dict[str, Any] = {"type": "noul", "instructions": instructions}
        if criteria is not None:
            wire["criteria"] = criteria
        answers = self._ask_questions(
            state,
            {MATCH_QUESTION_ID: Noul(instructions=instructions, criteria=criteria)},
            {MATCH_QUESTION_ID: wire},
        )
        return _extract_noul(_require_answer(answers, MATCH_QUESTION_ID))

    def _score_one(self, state: str, levels: list[str], instructions: str) -> float:
        answers = self._ask_questions(
            state,
            {SCORE_QUESTION_ID: Score(instructions=instructions, criteria=levels)},
            {
                SCORE_QUESTION_ID: {
                    "type": "score",
                    "instructions": instructions,
                    "criteria": levels,
                }
            },
        )
        return _extract_score(_require_answer(answers, SCORE_QUESTION_ID), levels)

    def _decisions_one(self, state: str, meta: Mapping[str, dict[str, Any]]) -> str:
        direct: dict[str, Any] = {}
        wire: dict[str, Any] = {}
        for question_id, spec in meta.items():
            if spec["type"] == "choice":
                instructions = spec.get("instructions", QUESTION_INSTRUCTIONS)
                direct[question_id] = Choice(
                    instructions=instructions, criteria=spec["choices"]
                )
                wire[question_id] = {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": spec["choices"],
                }
            elif spec["type"] == "noul":
                direct[question_id] = Noul(
                    instructions=spec["instructions"], criteria=spec["criteria"]
                )
                noul_wire: dict[str, Any] = {
                    "type": "noul",
                    "instructions": spec["instructions"],
                }
                if spec["criteria"] is not None:
                    noul_wire["criteria"] = spec["criteria"]
                wire[question_id] = noul_wire
            else:  # score
                direct[question_id] = Score(
                    instructions=spec["instructions"], criteria=spec["levels"]
                )
                wire[question_id] = {
                    "type": "score",
                    "instructions": spec["instructions"],
                    "criteria": spec["levels"],
                }

        answers = self._ask_questions(state, direct, wire)
        result: dict[str, Any] = {}
        for question_id, spec in meta.items():
            answer = _require_answer(answers, question_id)
            if spec["type"] == "choice":
                result[question_id] = _extract_choice(answer, spec["choices"])
            elif spec["type"] == "noul":
                result[question_id] = _extract_noul(answer)
            else:
                result[question_id] = _extract_score(answer, spec["levels"])
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    # -- transport ---------------------------------------------------------------

    def _ask_questions(
        self,
        state: Any,
        direct_questions: dict[str, Any],
        wire_questions: dict[str, dict[str, Any]],
    ) -> Any:
        try:
            if self.config.provider == "openrouter":
                return self._ask_openrouter(state, wire_questions)
            # The official TypeSafe SDK owns connection reuse and bounded
            # transient retries. Each worker thread uses its own client.
            client = self._get_typesafe_client()
            response = client.system_one(state=state, questions=direct_questions)
            return response.answers
        except Exception as exc:
            if isinstance(exc, JevError):
                raise
            raise _sanitised_error(exc, self.config) from None

    def _ask_openrouter(
        self, state: Any, wire_questions: dict[str, dict[str, Any]]
    ) -> Any:
        payload = json.dumps(
            {
                "model": self.config.model,
                "state": state,
                "questions": wire_questions,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        conn = self._get_openrouter_conn()
        started = time.monotonic()
        attempts = self.config.max_retries + 1

        for attempt in range(attempts):
            remaining = self.config.retry_budget - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            timeout = min(self.config.request_timeout, remaining)
            retry_after: float | None = None
            try:
                status, body, retry_after = conn.post(
                    OPENROUTER_DECISIONS_PATH, payload, headers, timeout
                )
            except TimeoutError:
                if attempt + 1 >= attempts:
                    raise TimeoutError from None
            except _ConnectionFailure:
                if attempt + 1 >= attempts:
                    raise
            else:
                if status == 200:
                    return _parse_openrouter_answers(body, wire_questions)
                if status not in OPENROUTER_RETRY_STATUSES or attempt + 1 >= attempts:
                    raise _HTTPFailure(status) from None

            delay = (
                retry_after if retry_after is not None else self._backoff_delay(attempt)
            )
            remaining = self.config.retry_budget - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            if delay:
                time.sleep(min(delay, remaining))

        raise AssertionError("unreachable")

    def _backoff_delay(self, attempt: int) -> float:
        base = min(
            self.config.backoff_initial * (2**attempt),
            self.config.backoff_max,
        )
        jitter = self.config.backoff_jitter
        if base <= 0 or jitter <= 0:
            return base
        return random.uniform(base * (1.0 - jitter), base)

    # -- worker-local resources --------------------------------------------------

    def _get_typesafe_client(self) -> TypeSafeClient:
        client = getattr(self._local, "typesafe_client", None)
        if client is None:
            retry = RetryPolicy(
                max_retries=self.config.max_retries,
                backoff_initial=self.config.backoff_initial,
                backoff_max=self.config.backoff_max,
                backoff_jitter=self.config.backoff_jitter,
                timeout=self.config.retry_budget,
            )
            try:
                client = TypeSafeClient(
                    api_key=self._api_key,
                    base_url=self.config.base_url,
                    model=self.config.model,
                    timeout=self.config.request_timeout,
                    retry=retry,
                )
            except Exception:  # noqa: BLE001 - convert SDK configuration failures safely
                raise JevError(
                    "Failed to initialise the Jev client; check runtime configuration"
                ) from None
            self._local.typesafe_client = client
            with self._registry_lock:
                self._typesafe_clients.append(client)
        return client

    def _get_openrouter_conn(self) -> _OpenRouterConn:
        conn = getattr(self._local, "openrouter_conn", None)
        if conn is None:
            conn = _OpenRouterConn(self.config.base_url)
            self._local.openrouter_conn = conn
            with self._registry_lock:
                self._openrouter_conns.append(conn)
        return conn

    def _get_executor(self) -> ThreadPoolExecutor:
        executor = self._executor
        if executor is None:
            with self._registry_lock:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self.config.max_concurrency,
                        thread_name_prefix="dbt-jev",
                    )
                executor = self._executor
        return executor

    # -- concurrency + de-duplication --------------------------------------------

    def _batch(
        self,
        rows: list[Any],
        *,
        is_null: Callable[[Any], bool],
        key: Callable[[Any], Any],
        work: Callable[[Any], Any],
    ) -> list[Any]:
        results: list[Any] = [None] * len(rows)
        representative: dict[Any, Any] = {}
        groups: dict[Any, list[int]] = {}
        for index, row in enumerate(rows):
            if is_null(row):
                continue
            row_key = key(row)
            if row_key in groups:
                groups[row_key].append(index)
            else:
                groups[row_key] = [index]
                representative[row_key] = row

        if not groups:
            return results

        if len(groups) == 1 or self.config.max_concurrency == 1:
            for row_key, indices in groups.items():
                value = work(representative[row_key])
                for index in indices:
                    results[index] = value
            return results

        executor = self._get_executor()
        futures = {
            executor.submit(work, representative[row_key]): row_key
            for row_key in groups
        }
        for future in as_completed(futures):
            value = future.result()  # first failure aborts the whole batch
            for index in groups[futures[future]]:
                results[index] = value
        return results


def _require_answer(answers: Any, question_id: str) -> Any:
    try:
        answer = answers[question_id]
    except (KeyError, TypeError, IndexError):
        raise JevError("Jev returned a malformed response") from None
    if answer is None:
        raise JevError("Jev returned a malformed response")
    return answer


def _extract_choice(answer: Any, criteria: Mapping[str, str]) -> str:
    label = _answer_field(answer, "choice")
    if not isinstance(label, str):
        raise JevError("Jev returned a malformed Choice response: choice is not text")
    if label not in criteria:
        raise JevError("Jev returned a Choice label outside the supplied criteria")
    return label


def _extract_noul(answer: Any) -> float:
    probability = _answer_number(answer, "noul", "Noul")
    if not 0.0 <= probability <= 1.0:
        raise JevError("Jev returned a Noul probability outside the range 0 to 1")
    return probability


def _extract_score(answer: Any, levels: Sequence[str]) -> float:
    result = _answer_number(answer, "score", "Score")
    if not 0.0 <= result <= len(levels) - 1:
        raise JevError("Jev returned a Score outside the supplied rubric")
    return result


def _parse_openrouter_answers(
    body: bytes, wire_questions: Mapping[str, dict[str, Any]]
) -> Any:
    try:
        decoded = json.loads(body)
        answers = decoded["answers"]
        for question_id, question in wire_questions.items():
            if answers[question_id].get("type") != question["type"]:
                raise KeyError("type")
        return answers
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError):
        raise JevError("Jev returned a malformed response") from None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        try:
            return max(0.0, float(value))
        except ValueError:
            return None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    now = datetime.datetime.now(when.tzinfo) if when.tzinfo else datetime.datetime.now()
    return max(0.0, (when - now).total_seconds())


def _answer_field(answer: Any, field: str) -> Any:
    if isinstance(answer, Mapping):
        return answer.get(field)
    return getattr(answer, field, None)


def _answer_number(answer: Any, field: str, primitive: str) -> float:
    value = _answer_field(answer, field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevError(
            f"Jev returned a malformed {primitive} response: {field} is not numeric"
        )
    result = float(value)
    if not math.isfinite(result):
        raise JevError(
            f"Jev returned a malformed {primitive} response: {field} is not finite"
        )
    return result


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

    if state is None:
        return None
    # Validate before initialising the client so bad criteria cannot be hidden by a
    # missing credential and can never result in an HTTP request.
    criteria = validate_choices(choices)
    return _get_default_classifier()._classify_one(str(state), criteria)


def match_probability(
    left: str | None,
    right: str | None,
    instructions: str,
    criteria: str | Mapping[str, Any] | None = None,
) -> float | None:
    """Convenience Noul entry point for retained module runtimes."""

    if left is None or right is None:
        return None
    validated_instructions = validate_instructions(instructions, "Noul")
    validated_criteria = validate_noul_criteria(criteria)
    return _get_default_classifier()._match_one(
        str(left), str(right), validated_instructions, validated_criteria
    )


def score(
    state: str | None, levels: str | Sequence[Any], instructions: str
) -> float | None:
    """Convenience Score entry point for retained module runtimes."""

    if state is None:
        return None
    validated_levels = validate_levels(levels)
    validated_instructions = validate_instructions(instructions, "Score")
    return _get_default_classifier()._score_one(
        str(state), validated_levels, validated_instructions
    )


def decisions(state: str | None, questions: str | Mapping[str, Any]) -> str | None:
    """Convenience multi-question entry point for retained module runtimes."""

    if state is None:
        return None
    meta = validate_questions(questions)
    return _get_default_classifier()._decisions_one(str(state), meta)


def _get_default_classifier() -> JevClassifier:
    global _default_classifier
    if _default_classifier is None:
        with _default_lock:
            if _default_classifier is None:
                _default_classifier = JevClassifier.from_env()
    return _default_classifier
