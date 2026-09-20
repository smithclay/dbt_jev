from __future__ import annotations

import threading

import pytest

from dbt_jev.runtime import JevClassifier, JevCriteriaError, JevError, RuntimeConfig
from tests.mock_service import create_server

CHOICES = {
    "expected": "An expected miss during exploration",
    "unexpected": "An actual malfunction",
    "unknown": "Insufficient evidence",
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


def make_classifier(mock_server, provider="typesafe", **overrides):
    host, port = mock_server.server_address
    values = {
        "provider": provider,
        "base_url": f"http://{host}:{port}",
        "model": "typesafe/jev-1.13" if provider == "openrouter" else "jev-latest",
        "request_timeout": 1.0,
        "max_retries": 2,
        "backoff_initial": 0.0,
        "backoff_max": 0.0,
        "retry_budget": 2.0,
    }
    values.update(overrides)
    return JevClassifier(RuntimeConfig(**values), api_key="mock-key")


def requests_for(mock_server):
    return mock_server.fixture_state.requests


def test_openrouter_env_defaults(monkeypatch):
    for name in (
        "DBT_JEV_BASE_URL",
        "DBT_JEV_MODEL",
        "OPENROUTER_BASE_URL",
        "OPENROUTER_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    config = RuntimeConfig.from_env({"provider": "openrouter"})
    assert config.base_url == "https://openrouter.ai"
    assert config.model == "typesafe/jev-1.13"


def test_provider_requests_have_equivalent_decision_semantics(mock_server):
    state = "Context with apostrophe's and 雪"
    direct = make_classifier(mock_server, "typesafe")
    routed = make_classifier(mock_server, "openrouter")
    assert direct.classify(state, CHOICES) == routed.classify(state, CHOICES)
    direct_request, routed_request = requests_for(mock_server)
    assert direct_request["body"]["state"] == routed_request["body"]["state"]
    assert direct_request["body"]["questions"] == routed_request["body"]["questions"]


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_null_input_returns_null_without_request(mock_server, provider):
    client = make_classifier(mock_server, provider)
    assert client.classify(None, CHOICES) is None
    assert requests_for(mock_server) == []


@pytest.mark.parametrize(
    "criteria",
    [
        "not json",
        "[]",
        "{}",
        '{"only":"one"}',
        {"": "description"},
        {"a": "", "b": "ok"},
    ],
)
def test_invalid_criteria_fails_before_request(mock_server, criteria):
    client = make_classifier(mock_server)
    with pytest.raises(JevCriteriaError):
        client.classify("would otherwise be sent", criteria)
    assert requests_for(mock_server) == []


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_quotes_and_unicode_are_preserved(mock_server, provider):
    client = make_classifier(mock_server, provider)
    state = "Couldn't find café/雪 — user's path was 'résumé.txt'"
    choices = {"isn't": "Apostrophe's label", "雪": "Unicode description"}
    assert client.classify(state, choices) == "isn't"
    sent = requests_for(mock_server)[0]["body"]
    assert sent["state"] == state
    assert sent["questions"]["classification"]["criteria"] == choices
    expected_path = (
        "/api/alpha/decisions" if provider == "openrouter" else "/v1/systemone"
    )
    assert requests_for(mock_server)[0]["path"] == expected_path


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_out_of_set_label_is_an_error(mock_server, provider):
    client = make_classifier(mock_server, provider)
    with pytest.raises(JevError, match="outside the supplied criteria"):
        client.classify("fixture:out_of_set", CHOICES)


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_malformed_response_is_sanitised(mock_server, provider):
    client = make_classifier(mock_server, provider)
    with pytest.raises(JevError, match="malformed response") as caught:
        client.classify("fixture:malformed", CHOICES)
    assert "not-json" not in str(caught.value)


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_authentication_failure_is_sanitised(mock_server, provider):
    host, port = mock_server.server_address
    client = JevClassifier(
        RuntimeConfig(
            provider=provider,
            base_url=f"http://{host}:{port}",
            model="typesafe/jev-1.13" if provider == "openrouter" else "jev-latest",
            max_retries=0,
            backoff_initial=0,
            backoff_max=0,
        ),
        api_key="do-not-leak-this",
    )
    with pytest.raises(JevError, match="authentication failed") as caught:
        client.classify("state", CHOICES)
    assert "do-not-leak-this" not in str(caught.value)
    expected_credential = (
        "OPENROUTER_API_KEY" if provider == "openrouter" else "TYPESAFE_API_KEY"
    )
    assert expected_credential in str(caught.value)
    assert len(requests_for(mock_server)) == 1


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_timeout_is_bounded_and_sanitised(mock_server, provider):
    client = make_classifier(
        mock_server,
        provider,
        request_timeout=0.03,
        max_retries=1,
        retry_budget=0.2,
    )
    with pytest.raises(JevError, match="timed out"):
        client.classify("fixture:timeout", CHOICES)
    assert 1 <= len(requests_for(mock_server)) <= 2


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_transient_failures_retry_then_succeed(mock_server, provider):
    client = make_classifier(mock_server, provider)
    assert client.classify("fixture:transient", CHOICES) == "expected"
    assert len(requests_for(mock_server)) == 3


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_exhausted_transient_retries_are_bounded(mock_server, provider):
    client = make_classifier(mock_server, provider)
    with pytest.raises(JevError, match="HTTP 529.*3 attempts"):
        client.classify("fixture:always_transient", CHOICES)
    assert len(requests_for(mock_server)) == 3
