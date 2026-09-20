"""dbt-duckdb plugin that registers the scalar ``jev_classify`` SQL function."""

from __future__ import annotations

import threading
from typing import Any

from dbt.adapters.duckdb.plugins import BasePlugin

from .runtime import JevClassifier, RuntimeConfig, validate_choices


class Plugin(BasePlugin):
    def initialize(self, config: dict[str, Any] | None = None) -> None:
        # Plugin configuration is intentionally non-secret. The selected provider's
        # API key is read from the dbt runner environment when SQL first calls Jev.
        self._config = RuntimeConfig.from_env(config or {})

    def configure_connection(self, connection: Any) -> None:
        classifier: JevClassifier | None = None
        initialise_lock = threading.Lock()

        def classify(state: str | None, choices_json: str) -> str | None:
            nonlocal classifier
            if state is None:
                return None
            criteria = validate_choices(choices_json)
            if classifier is None:
                with initialise_lock:
                    if classifier is None:
                        classifier = JevClassifier(self._config)
            return classifier.classify(state, criteria)

        connection.create_function(
            "jev_classify",
            classify,
            ["VARCHAR", "VARCHAR"],
            "VARCHAR",
            side_effects=True,
        )
