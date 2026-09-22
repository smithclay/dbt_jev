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


def test_json_each_row_dispatches_match_and_score(monkeypatch):
    calls = []

    def fake_match(left, right, instructions, criteria):
        calls.append(("match", left, right, instructions, criteria))
        return 0.875

    def fake_score(state, levels, instructions):
        calls.append(("score", state, levels, instructions))
        return 1.75

    monkeypatch.setattr(clickhouse_udf, "match_probability", fake_match)
    monkeypatch.setattr(clickhouse_udf, "score", fake_score)
    stdin = io.StringIO(
        "".join(
            [
                json.dumps(
                    {
                        "left": "O'Brien",
                        "right": "Obrien",
                        "instructions": "Same person?",
                        "criteria": '{"true":"Same","false":"Different"}',
                    }
                )
                + "\n",
                json.dumps(
                    {
                        "input": "urgent message",
                        "levels": '["Low","Medium","High"]',
                        "instructions": "Rate urgency",
                    }
                )
                + "\n",
            ]
        )
    )
    stdout = io.StringIO()

    clickhouse_udf.serve(stdin, stdout)

    assert [json.loads(line) for line in stdout.getvalue().splitlines()] == [
        {"result": 0.875},
        {"result": 1.75},
    ]
    assert calls == [
        (
            "match",
            "O'Brien",
            "Obrien",
            "Same person?",
            '{"true":"Same","false":"Different"}',
        ),
        (
            "score",
            "urgent message",
            '["Low","Medium","High"]',
            "Rate urgency",
        ),
    ]


def test_json_each_row_dispatches_decisions(monkeypatch):
    calls = []

    def fake_decisions(state, questions):
        calls.append((state, questions))
        return None if state is None else '{"failure_type":"expected"}'

    monkeypatch.setattr(clickhouse_udf, "decisions", fake_decisions)
    questions = json.dumps(
        {"failure_type": {"type": "choice", "choices": {"expected": "d", "other": "e"}}}
    )
    stdin = io.StringIO(
        "".join(
            json.dumps({"input": state, "questions": questions}) + "\n"
            for state in ["No file found", None]
        )
    )
    stdout = io.StringIO()

    clickhouse_udf.serve(stdin, stdout)

    assert [json.loads(line) for line in stdout.getvalue().splitlines()] == [
        {"result": '{"failure_type":"expected"}'},
        {"result": None},
    ]
    assert calls == [("No file found", questions), (None, questions)]


@pytest.mark.parametrize(
    "row",
    [
        [],
        {},
        {"input": "value"},
        {"input": 3, "choices": "{}"},
        {"left": "a", "right": "b"},
        {"input": "value", "levels": "[]"},
        {"questions": "{}"},
        {"input": 3, "questions": "{}"},
    ],
)
def test_malformed_rows_raise_safe_errors(row):
    with pytest.raises(JevError, match="ClickHouse"):
        clickhouse_udf.process_row(row)


def test_malformed_json_is_not_echoed():
    with pytest.raises(JevError, match="malformed JSON") as caught:
        clickhouse_udf.serve(io.StringIO("{secret-not-json\n"), io.StringIO())
    assert "secret-not-json" not in str(caught.value)
