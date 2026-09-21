"""Deterministic Jev-shaped HTTP fixture service used by unit and dbt tests."""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FixtureState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.attempts: Counter[str] = Counter()

    def reset(self) -> None:
        with self.lock:
            self.requests.clear()
            self.attempts.clear()


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    state = FixtureState()

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, value: Any) -> None:
            encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            if self.path == "/_requests":
                with state.lock:
                    self._send_json(200, {"requests": list(state.requests)})
                return
            self._send_json(404, {"detail": "not found"})

        def do_POST(self) -> None:
            if self.path == "/_reset":
                state.reset()
                self._send_json(200, {"ok": True})
                return
            if self.path not in {"/v1/systemone", "/api/alpha/decisions"}:
                self._send_json(404, {"detail": "not found"})
                return

            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self._send_json(400, {"detail": "invalid json"})
                return

            state_text = str(body.get("state", ""))
            with state.lock:
                state.attempts[state_text] += 1
                attempt = state.attempts[state_text]
                state.requests.append(
                    {
                        "path": self.path,
                        "authorised": self.headers.get("Authorization")
                        == "Bearer mock-key",
                        "body": body,
                    }
                )

            if self.headers.get("Authorization") != "Bearer mock-key":
                self._send_json(401, {"detail": "invalid credential"})
                return
            if "fixture:timeout" in state_text:
                time.sleep(0.25)
            if "fixture:always_transient" in state_text:
                self._send_json(529, {"detail": "overloaded"})
                return
            if "fixture:transient" in state_text and attempt <= 2:
                self._send_json(429 if attempt == 1 else 529, {"detail": "retry"})
                return
            if "fixture:malformed" in state_text:
                encoded = b"{not-json"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return

            questions = body.get("questions", {})
            if not isinstance(questions, dict) or len(questions) != 1:
                self._send_json(400, {"detail": "expected one question"})
                return
            question_id, question = next(iter(questions.items()))
            answer = _answer_fixture(state_text, question)
            self._send_json(
                200,
                {
                    "model": "jev-fixture-1",
                    "answers": {question_id: answer},
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            )

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.fixture_state = state  # type: ignore[attr-defined]
    return server


def _pick_label(state: str, criteria: dict[str, Any]) -> str:
    lowered = state.lower()
    if "no file found" in lowered and "expected" in criteria:
        return "expected"
    if (
        "authentication failure" in lowered or "parsing error" in lowered
    ) and "unexpected" in criteria:
        return "unexpected"
    if "fixture:transient" in lowered and "expected" in criteria:
        return "expected"
    if "unknown" in criteria:
        return "unknown"
    return next(iter(criteria), "missing")


def _answer_fixture(state: str, question: dict[str, Any]) -> dict[str, Any]:
    question_type = question.get("type")
    if question_type == "choice":
        criteria = question.get("criteria", {})
        label = _pick_label(state, criteria)
        if "fixture:out_of_set" in state:
            label = "not-supplied"
        probabilities = {key: (1.0 if key == label else 0.0) for key in criteria}
        return {
            "type": "choice",
            "choice": label,
            "probabilities": probabilities,
            "confidence": 1.0,
        }
    if question_type == "noul":
        return {
            "type": "noul",
            "noul": 1.5 if "fixture:out_of_range" in state else 0.875,
        }
    if question_type == "score":
        levels = question.get("criteria", [])
        result = 1.75 if len(levels) >= 3 else float(len(levels) - 1)
        if "fixture:out_of_range" in state:
            result = float(len(levels))
        return {
            "type": "score",
            "score": result,
            "confidence": 0.8,
            "legend": {str(index): level for index, level in enumerate(levels)},
            "probabilities": {
                str(index): (1.0 if index == round(result) else 0.0)
                for index in range(len(levels))
            },
        }
    return {"type": "unsupported"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    create_server(args.host, args.port).serve_forever()


if __name__ == "__main__":
    main()
