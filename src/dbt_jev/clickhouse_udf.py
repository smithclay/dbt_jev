"""ClickHouse executable-UDF process for ``jev_classify``.

ClickHouse sends and receives one JSONEachRow object per row. Module globals in
``runtime`` retain the HTTP client for the lifetime of the executable-pool worker.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .runtime import JevError, classify


def process_row(row: Any) -> dict[str, str | None]:
    """Classify one decoded JSONEachRow value."""

    if not isinstance(row, dict):
        raise JevError("ClickHouse sent a malformed executable-UDF row")
    if "input" not in row or "choices" not in row:
        raise JevError("ClickHouse executable-UDF row is missing an argument")
    state = row["input"]
    choices = row["choices"]
    if state is not None and not isinstance(state, str):
        raise JevError("ClickHouse executable-UDF input must be text or NULL")
    if not isinstance(choices, str):
        raise JevError("ClickHouse executable-UDF choices must be JSON text")
    return {"result": classify(state, choices)}


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
