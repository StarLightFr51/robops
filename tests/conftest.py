import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from robops.client import OpenCloudClient  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.reason = "fake"
        self.text = body if isinstance(body, str) else json.dumps(body if body is not None else {})

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body if self._body is not None else {}


class FakeSession:
    """Session HTTP factice : renvoie les réponses dans l'ordre et enregistre les appels."""

    def __init__(self, responses=None, router=None):
        self.headers = {}
        self.calls = []
        self._responses = list(responses or [])
        self._router = router

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._router:
            return self._router(method, url, kwargs)
        return self._responses.pop(0)


@pytest.fixture
def make_client():
    def factory(responses=None, router=None):
        session = FakeSession(responses, router)
        sleeps = []
        client = OpenCloudClient("test-key", session=session, sleep=sleeps.append, backoff_base=0.01)
        return client, session, sleeps
    return factory


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "ROBLOX_API_KEY", "CI",
                "ROBOPS_REPO", "ROBOPS_NO_UPDATE", "ROBOPS_GITHUB_TOKEN", "GH_TOKEN", "ROBOPS_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
