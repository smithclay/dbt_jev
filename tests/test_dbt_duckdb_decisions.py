from __future__ import annotations

import threading

import duckdb
import pytest

from tests.mock_service import create_server
from tests.test_dbt_duckdb import _dbt, _env, _project


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


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_dbt_duckdb_decisions_batches_questions(tmp_path, mock_server, provider):
    project = _project(tmp_path)
    env = _env(mock_server, provider)
    requests = mock_server.fixture_state.requests

    _dbt(project, env, "deps")
    _dbt(project, env, "seed", "--target", "duckdb")
    _dbt(
        project,
        env,
        "run",
        "--target",
        "duckdb",
        "--select",
        "+batched_decisions",
        "--vars",
        "{dbt_jev_run_decisions: true}",
    )

    # One row, two questions -> exactly one request carrying both questions.
    assert len(requests) == 1
    body = requests[0]["body"]
    assert set(body["questions"]) == {"failure_type", "urgency"}

    db_path = project / "target" / "dbt_jev_integration_tests.duckdb"
    with duckdb.connect(str(db_path), read_only=True) as connection:
        failure_type, urgency = connection.execute(
            "select failure_type, urgency from batched_decisions"
        ).fetchone()
    assert failure_type == "expected"
    assert urgency == pytest.approx(1.75)

    # Reading the materialised table must not perform further inference.
    assert len(requests) == 1
