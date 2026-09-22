"""dbt-duckdb plugin that registers the vectorized Jev SQL functions.

The functions are registered as Arrow (vectorized) scalar UDFs: DuckDB hands the
whole data chunk to Python at once, so the runtime can de-duplicate identical
inputs and evaluate the distinct ones concurrently instead of blocking on one
request per row.
"""

from __future__ import annotations

import threading
from typing import Any

import pyarrow as pa
from dbt.adapters.duckdb.plugins import BasePlugin

from .runtime import JevClassifier, RuntimeConfig


def _scalar(array: Any) -> Any:
    """Return the first element of a constant argument array, or None if empty."""

    if len(array) == 0:
        return None
    return array.slice(0, 1).to_pylist()[0]


class Plugin(BasePlugin):
    def initialize(self, config: dict[str, Any] | None = None) -> None:
        # Plugin configuration is intentionally non-secret. The selected provider's
        # API key is read from the dbt runner environment when SQL first calls Jev.
        self._config = RuntimeConfig.from_env(config or {})

    def configure_connection(self, connection: Any) -> None:
        classifier: JevClassifier | None = None
        initialise_lock = threading.Lock()

        def get_classifier() -> JevClassifier:
            nonlocal classifier
            if classifier is None:
                with initialise_lock:
                    if classifier is None:
                        classifier = JevClassifier(self._config)
            return classifier

        def classify(state: pa.Array, choices: pa.Array) -> pa.Array:
            states = state.to_pylist()
            if not states:
                return pa.array([], type=pa.string())
            results = get_classifier().classify_many(states, _scalar(choices))
            return pa.array(results, type=pa.string())

        def match_probability(
            left: pa.Array,
            right: pa.Array,
            instructions: pa.Array,
            criteria: pa.Array,
        ) -> pa.Array:
            lefts = left.to_pylist()
            if not lefts:
                return pa.array([], type=pa.float64())
            results = get_classifier().match_probability_many(
                lefts,
                right.to_pylist(),
                _scalar(instructions),
                _scalar(criteria),
            )
            return pa.array(results, type=pa.float64())

        def score(
            state: pa.Array, levels: pa.Array, instructions: pa.Array
        ) -> pa.Array:
            states = state.to_pylist()
            if not states:
                return pa.array([], type=pa.float64())
            results = get_classifier().score_many(
                states, _scalar(levels), _scalar(instructions)
            )
            return pa.array(results, type=pa.float64())

        def decisions(state: pa.Array, questions: pa.Array) -> pa.Array:
            states = state.to_pylist()
            if not states:
                return pa.array([], type=pa.string())
            results = get_classifier().decisions_many(states, _scalar(questions))
            return pa.array(results, type=pa.string())

        connection.create_function(
            "jev_classify",
            classify,
            ["VARCHAR", "VARCHAR"],
            "VARCHAR",
            type="arrow",
            null_handling="special",
            side_effects=True,
        )
        connection.create_function(
            "jev_match_probability",
            match_probability,
            ["VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR"],
            "DOUBLE",
            type="arrow",
            null_handling="special",
            side_effects=True,
        )
        connection.create_function(
            "jev_score",
            score,
            ["VARCHAR", "VARCHAR", "VARCHAR"],
            "DOUBLE",
            type="arrow",
            null_handling="special",
            side_effects=True,
        )
        connection.create_function(
            "jev_decisions",
            decisions,
            ["VARCHAR", "VARCHAR"],
            "VARCHAR",
            type="arrow",
            null_handling="special",
            side_effects=True,
        )
