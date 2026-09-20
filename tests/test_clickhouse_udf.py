from __future__ import annotations

import io
import json

import pytest

from dbt_jev import clickhouse_udf
from dbt_jev.runtime import JevError


def test_json_each_row_preserves_quotes_unicode_and_null(monkeypatch):
    calls = []

    def fake_classify(state, choices):
        calls.append((state, choices))
        return None if state is None else "isn't"

    monkeypatch.setattr(clickhouse_udf, "classify", fake_classify)
    choices = json.dumps(
        {"isn't": "Apostrophe's label", "雪": "Unicode description"},
        ensure_ascii=False,
    )
    stdin = io.StringIO(
        "".join(
            json.dumps({"input": state, "choices": choices}, ensure_ascii=False) + "\n"
            for state in ["Couldn't find café/雪", None]
        )
    )
    stdout = io.StringIO()

    clickhouse_udf.serve(stdin, stdout)

    assert [json.loads(line) for line in stdout.getvalue().splitlines()] == [
        {"result": "isn't"},
        {"result": None},
    ]
    assert calls == [("Couldn't find café/雪", choices), (None, choices)]


@pytest.mark.parametrize(
    "row",
    [[], {}, {"input": "value"}, {"input": 3, "choices": "{}"}],
)
def test_malformed_rows_raise_safe_errors(row):
    with pytest.raises(JevError, match="ClickHouse"):
        clickhouse_udf.process_row(row)


def test_malformed_json_is_not_echoed():
    with pytest.raises(JevError, match="malformed JSON") as caught:
        clickhouse_udf.serve(io.StringIO("{secret-not-json\n"), io.StringIO())
    assert "secret-not-json" not in str(caught.value)
