"""ClickHouse executable-UDF process for the Jev scalar functions.

ClickHouse sends and receives one JSONEachRow object per row. Module globals in
``runtime`` retain the HTTP client for the lifetime of the executable-pool worker.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .runtime import JevError, classify, match_probability, score


def process_row(row: Any) -> dict[str, str | float | None]:
    """Evaluate one decoded JSONEachRow function call."""

    if not isinstance(row, dict):
        raise JevError("ClickHouse sent a malformed executable-UDF row")

    if "choices" in row:
        return _classify_row(row)
    if "levels" in row:
        return _score_row(row)
    if any(key in row for key in ("left", "right", "criteria")):
        return _match_row(row)
    raise JevError("ClickHouse executable-UDF row is missing an argument")


def _classify_row(row: dict[str, Any]) -> dict[str, str | None]:
    if "input" not in row:
        raise JevError("ClickHouse executable-UDF row is missing an argument")
    state = row["input"]
    choices = row["choices"]
    if state is not None and not isinstance(state, str):
        raise JevError("ClickHouse executable-UDF input must be text or NULL")
    if not isinstance(choices, str):
        raise JevError("ClickHouse executable-UDF choices must be JSON text")
    return {"result": classify(state, choices)}


def _match_row(row: dict[str, Any]) -> dict[str, float | None]:
    required = {"left", "right", "instructions", "criteria"}
    if not required <= row.keys():
        raise JevError("ClickHouse executable-UDF row is missing an argument")
    left = row["left"]
    right = row["right"]
    instructions = row["instructions"]
    criteria = row["criteria"]
    if left is not None and not isinstance(left, str):
        raise JevError("ClickHouse executable-UDF left input must be text or NULL")
    if right is not None and not isinstance(right, str):
        raise JevError("ClickHouse executable-UDF right input must be text or NULL")
    if not isinstance(instructions, str) or not isinstance(criteria, str):
        raise JevError(
            "ClickHouse executable-UDF match configuration must be JSON/text"
        )
    return {"result": match_probability(left, right, instructions, criteria)}


def _score_row(row: dict[str, Any]) -> dict[str, float | None]:
    required = {"input", "levels", "instructions"}
    if not required <= row.keys():
        raise JevError("ClickHouse executable-UDF row is missing an argument")
    state = row["input"]
    levels = row["levels"]
    instructions = row["instructions"]
    if state is not None and not isinstance(state, str):
        raise JevError("ClickHouse executable-UDF input must be text or NULL")
    if not isinstance(levels, str) or not isinstance(instructions, str):
        raise JevError(
            "ClickHouse executable-UDF score configuration must be JSON/text"
        )
    return {"result": score(state, levels, instructions)}


def serve(stdin: TextIO, stdout: TextIO) -> None:
    """Serve JSONEachRow records until ClickHouse closes stdin."""

    for line in stdin:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            raise JevError(
                "ClickHouse sent malformed JSON to the executable UDF"
            ) from None
        result = process_row(row)
        stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        stdout.write("\n")
        stdout.flush()


def main() -> int:
    try:
        serve(sys.stdin, sys.stdout)
    except Exception as exc:  # noqa: BLE001 - executable boundary must suppress traceback
        message = (
            str(exc) if isinstance(exc, JevError) else "unexpected runtime failure"
        )
        sys.stderr.write(f"dbt_jev executable UDF failed: {message}\n")
        sys.stderr.flush()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
