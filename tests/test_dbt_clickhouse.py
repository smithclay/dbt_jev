from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import clickhouse_connect
import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
DBT = str(Path(sys.executable).with_name("dbt"))
EXPECTED = {
    "span-001": "expected",
    "span-002": "unexpected",
    "span-003": "unexpected",
    "span-004": "unknown",
}


def _http_json(url: str, *, method: str = "GET"):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "integration_tests"
    shutil.copytree(ROOT / "integration_tests", project)
    (project / "packages.yml").write_text(
        f"packages:\n  - local: {ROOT.as_posix()}\n", encoding="utf-8"
    )
    return project


def _dbt(project: Path, env: dict[str, str], target: str, *args: str):
    result = subprocess.run(
        [DBT, "--no-use-colors", *args, "--target", target],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"dbt {' '.join(args)} --target {target} failed:\n"
            f"{result.stdout}\n{result.stderr}"
        )


def _normalised_requests(base_url: str):
    bodies = [item["body"] for item in _http_json(base_url + "/_requests")["requests"]]
    return sorted(bodies, key=lambda body: body["state"])


@pytest.mark.clickhouse
@pytest.mark.skipif(
    os.getenv("DBT_JEV_TEST_CLICKHOUSE") != "1",
    reason="set DBT_JEV_TEST_CLICKHOUSE=1 after starting docker compose",
)
def test_duckdb_and_clickhouse_send_and_materialise_the_same_values(tmp_path):
    project = _project(tmp_path)
    mock_url = os.getenv("DBT_JEV_MOCK_URL", "http://127.0.0.1:58080")
    provider = os.getenv("DBT_JEV_TEST_PROVIDER", "typesafe")
    env = {
        **os.environ,
        "DBT_JEV_PROVIDER": provider,
        "DBT_JEV_BASE_URL": mock_url,
        "DBT_JEV_MODEL": "typesafe/jev-1.13"
        if provider == "openrouter"
        else "jev-latest",
        "DBT_JEV_BACKOFF_INITIAL": "0",
        "DBT_JEV_BACKOFF_MAX": "0",
        "DBT_PROFILES_DIR": ".",
        "DBT_ENV_SECRET_CLICKHOUSE_PASSWORD": "dbt",
    }
    credential = (
        "OPENROUTER_API_KEY" if provider == "openrouter" else "TYPESAFE_API_KEY"
    )
    env[credential] = "mock-key"
    subprocess.run([DBT, "--no-use-colors", "deps"], cwd=project, env=env, check=True)

    _http_json(mock_url + "/_reset", method="POST")
    _dbt(project, env, "duckdb", "parse")
    _dbt(project, env, "duckdb", "compile")
    assert _normalised_requests(mock_url) == []
    _dbt(project, env, "duckdb", "seed")
    _dbt(project, env, "duckdb", "run")
    duckdb_requests = _normalised_requests(mock_url)
    with duckdb.connect(
        str(project / "target" / "dbt_jev_integration_tests.duckdb"), read_only=True
    ) as connection:
        duckdb_rows = dict(
            connection.execute(
                "select span_id, failure_type from classified_tool_calls order by span_id"
            ).fetchall()
        )
        duckdb_null_result = connection.execute(
            "select classification from quoted_and_null"
        ).fetchone()[0]

    _http_json(mock_url + "/_reset", method="POST")
    clickhouse_compile_env = {
        key: value
        for key, value in env.items()
        if key not in {"TYPESAFE_API_KEY", "OPENROUTER_API_KEY"}
    }
    _dbt(project, clickhouse_compile_env, "clickhouse", "parse")
    _dbt(project, clickhouse_compile_env, "clickhouse", "compile")
    assert _normalised_requests(mock_url) == []
    _dbt(project, clickhouse_compile_env, "clickhouse", "seed")
    _dbt(project, clickhouse_compile_env, "clickhouse", "run")
    clickhouse_requests = _normalised_requests(mock_url)
    assert clickhouse_requests == duckdb_requests
    expected_path = (
        "/api/alpha/decisions" if provider == "openrouter" else "/v1/systemone"
    )
    recorded = _http_json(mock_url + "/_requests")["requests"]
    assert all(item["path"] == expected_path for item in recorded)

    client = clickhouse_connect.get_client(
        host=env.get("DBT_JEV_CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(env.get("DBT_JEV_CLICKHOUSE_PORT", "58124")),
        username="dbt",
        password="dbt",
        database="default",
    )
    try:
        rows = dict(
            client.query(
                "select span_id, failure_type from classified_tool_calls order by span_id"
            ).result_rows
        )
        null_result = client.query(
            "select classification from quoted_and_null"
        ).first_row[0]
    finally:
        client.close()
    assert rows == EXPECTED
    assert duckdb_rows == rows
    assert null_result is None
    assert duckdb_null_result is None
    assert _normalised_requests(mock_url) == clickhouse_requests
    _dbt(project, clickhouse_compile_env, "clickhouse", "test")
    assert _normalised_requests(mock_url) == clickhouse_requests

    artifact_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (project / "target").rglob("*")
        if path.is_file() and path.suffix != ".duckdb"
    )
    assert "mock-key" not in artifact_text
