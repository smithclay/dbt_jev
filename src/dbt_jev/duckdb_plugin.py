"""dbt-duckdb plugin that registers the scalar Jev SQL functions."""

from __future__ import annotations

import threading
from typing import Any

from dbt.adapters.duckdb.plugins import BasePlugin

from .runtime import (
    JevClassifier,
    RuntimeConfig,
    validate_choices,
    validate_instructions,
    validate_levels,
    validate_noul_criteria,
)


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

        def classify(state: str | None, choices_json: str) -> str | None:
            if state is None:
                return None
            criteria = validate_choices(choices_json)
            return get_classifier().classify(state, criteria)

        def match_probability(
            left: str | None,
            right: str | None,
            instructions: str,
            criteria_json: str,
        ) -> float | None:
            if left is None or right is None:
                return None
            validated_instructions = validate_instructions(instructions, "Noul")
            criteria = validate_noul_criteria(criteria_json)
            return get_classifier().match_probability(
                left, right, validated_instructions, criteria
            )

        def score(
            state: str | None, levels_json: str, instructions: str
        ) -> float | None:
            if state is None:
                return None
            levels = validate_levels(levels_json)
            validated_instructions = validate_instructions(instructions, "Score")
            return get_classifier().score(state, levels, validated_instructions)

        connection.create_function(
            "jev_classify",
            classify,
            ["VARCHAR", "VARCHAR"],
            "VARCHAR",
            side_effects=True,
        )
        connection.create_function(
            "jev_match_probability",
            match_probability,
            ["VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR"],
            "DOUBLE",
            side_effects=True,
        )
        connection.create_function(
            "jev_score",
            score,
            ["VARCHAR", "VARCHAR", "VARCHAR"],
            "DOUBLE",
            side_effects=True,
        )
