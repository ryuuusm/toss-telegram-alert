"""Fake HTTP transport shared by tests: scripted responses, recorded requests, no network."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeTransport:
    def __init__(self):
        self.routes = {}      # (method, path-prefix) -> list of (status, headers, body)
        self.calls = []       # (method, url, headers, body)

    def add(self, method, path, status=200, body=None, headers=None):
        self.routes.setdefault((method, path), []).append(
            (status, headers or {}, json.dumps(body).encode() if body is not None else b""))

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        path = url.split("openapi.tossinvest.com", 1)[-1].split("?", 1)[0]
        if "api.telegram.org" in url:
            path = "telegram"
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected request {method} {path}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


@pytest.fixture
def fake():
    return FakeTransport()


@pytest.fixture
def no_sleep():
    return {"sleep": lambda s: None, "rand": lambda: 0.0}
