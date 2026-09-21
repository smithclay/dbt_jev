from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import duckdb
import pytest

from tests.mock_service import create_server

ROOT = Path(__file__).resolve().parents[1]
DBT = str(Path(sys.executable).with_name("dbt"))
EXPECTED = {
    "span-001": "expected",
    "span-002": "unexpected",
    "span-003": "unexpected",
    "span-004": "unknown",
}


@pytest.fixture()
def mock_server():
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "integration_tests"
    shutil.copytree(
        ROOT / "integration_tests",
        project,
        ignore=shutil.ignore_patterns("dbt_packages", "target", "logs", ".user.yml"),
    )
    (project / "packages.yml").write_text(
        f"packages:\n  - local: {ROOT.as_posix()}\n", encoding="utf-8"
    )
    return project


def _dbt(project: Path, env: dict[str, str], *args: str, ok: bool = True):
    result = subprocess.run(
        [DBT, "--no-use-colors", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if ok and result.returncode != 0:
        raise AssertionError(
            f"dbt {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )
    return result


def _env(mock_server, provider: str) -> dict[str, str]:
    host, port = mock_server.server_address
    env = {
        **os.environ,
        "DBT_JEV_PROVIDER": provider,
        "DBT_JEV_BASE_URL": f"http://{host}:{port}",
        "DBT_JEV_MODEL": "typesafe/jev-1.13"
        if provider == "openrouter"
        else "jev-latest",
        "DBT_JEV_BACKOFF_INITIAL": "0",
        "DBT_JEV_BACKOFF_MAX": "0",
        "DBT_JEV_RETRY_BUDGET": "2",
        "DBT_PROFILES_DIR": ".",
    }
    credential = (
        "OPENROUTER_API_KEY" if provider == "openrouter" else "TYPESAFE_API_KEY"
    )
    env[credential] = "mock-key"
    return env


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_dbt_duckdb_materialises_without_compile_time_or_read_time_calls(
    tmp_path, mock_server, provider
):
    project = _project(tmp_path)
    env = _env(mock_server, provider)
    requests = mock_server.fixture_state.requests

    _dbt(project, env, "deps")
    credential = (
        "OPENROUTER_API_KEY" if provider == "openrouter" else "TYPESAFE_API_KEY"
    )
    compile_env = {key: value for key, value in env.items() if key != credential}
    _dbt(project, compile_env, "parse", "--target", "duckdb")
    _dbt(project, compile_env, "compile", "--target", "duckdb")
    assert requests == []

    _dbt(project, env, "seed", "--target", "duckdb")
    assert requests == []
    _dbt(project, env, "run", "--target", "duckdb")
    assert len(requests) == 6

    db_path = project / "target" / "dbt_jev_integration_tests.duckdb"
    with duckdb.connect(str(db_path), read_only=True) as connection:
        rows = dict(
            connection.execute(
                "select span_id, failure_type from classified_tool_calls order by span_id"
            ).fetchall()
        )
        summary_count = connection.execute(
            "select count(*) from tool_failure_summary"
        ).fetchone()[0]
        null_results = connection.execute(
            "select classification, match_probability, score from quoted_and_null"
        ).fetchone()
        decision_results = connection.execute(
            "select match_probability, urgency_score from decision_primitives"
        ).fetchone()
    assert rows == EXPECTED
    assert summary_count == 4
    assert null_results == (None, None, None)
    assert decision_results == pytest.approx((0.875, 1.75))
    assert len(requests) == 6, "reading the materialised tables performed inference"

    sent = [request["body"] for request in requests]
    expected_model = "typesafe/jev-1.13" if provider == "openrouter" else "jev-latest"
    assert all(body["model"] == expected_model for body in sent)
    expected_path = (
        "/api/alpha/decisions" if provider == "openrouter" else "/v1/systemone"
    )
    assert all(request["path"] == expected_path for request in requests)
    choice_bodies = [body for body in sent if "classification" in body["questions"]]
    match_bodies = [body for body in sent if "match" in body["questions"]]
    score_bodies = [body for body in sent if "score" in body["questions"]]
    assert len(choice_bodies) == 4
    assert len(match_bodies) == 1
    assert len(score_bodies) == 1
    assert all(
        body["questions"]["classification"]["criteria"]
        == {
            "expected": "An expected miss during exploration",
            "unexpected": "An actual malfunction",
            "unknown": "Insufficient evidence",
        }
        for body in choice_bodies
    )
    assert match_bodies[0]["state"] == {
        "left": "file_lookup",
        "right": (
            "tool=file_lookup; request=Look for optional project instructions; "
            "result=No file found; exploration continued normally"
        ),
    }
    assert match_bodies[0]["questions"]["match"] == {
        "type": "noul",
        "instructions": (
            "Treat the two-character sequence \\n literally; do these records "
            "represent the same customer's account?"
        ),
        "criteria": {
            "true": "Both records identify the same customer; preserve \\n literally",
            "false": "The records identify different customers",
        },
    }
    assert score_bodies[0]["questions"]["score"] == {
        "type": "score",
        "instructions": "Rate the urgency of this request; preserve \\n literally",
        "criteria": [
            "No urgency; preserve \\n literally",
            "Needs attention",
            "Urgent",
        ],
    }

    _dbt(project, env, "test", "--target", "duckdb")
    assert len(requests) == 6

    invalid = _dbt(project, env, "run-operation", "compile_invalid_criteria", ok=False)
    assert invalid.returncode != 0
    assert "at least two labels" in invalid.stdout + invalid.stderr
    assert len(requests) == 6

    invalid_match = _dbt(
        project, env, "run-operation", "compile_invalid_match_criteria", ok=False
    )
    assert invalid_match.returncode != 0
    assert "only supports the labels" in invalid_match.stdout + invalid_match.stderr
    assert len(requests) == 6

    invalid_score = _dbt(
        project, env, "run-operation", "compile_invalid_score_levels", ok=False
    )
    assert invalid_score.returncode != 0
    assert "at least one description" in invalid_score.stdout + invalid_score.stderr
    assert len(requests) == 6

    artifact_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (project / "target").rglob("*")
        if path.is_file()
    )
    assert "mock-key" not in artifact_text
