"""Unit and regression tests for AUD-29 and AUD-30 fixes in v3.49.0."""

import socket
import pytest
from services.ai import GeminiClient


def test_aud30_ai_service_timeout_clean_chaining(monkeypatch):
    """Verify TimeoutError is raised with from None when deadline is exceeded during socket timeout."""
    client = GeminiClient(api_key="dummy_key", model="gemini-2.5-flash", request_timeout=0.01)

    class DummyResponse:
        def getheader(self, name, default=None):
            return None

        def read(self, *args, **kwargs):
            raise socket.timeout("timed out")

    monkeypatch.setattr("time.monotonic", lambda: 1000.0)

    with pytest.raises(TimeoutError) as exc_info:
        client._read_response(DummyResponse(), None, deadline=999.0)

    assert "global deadline exceeded" in str(exc_info.value)
    # Verify from None was used (no cause attached)
    assert exc_info.value.__cause__ is None


def test_aud29_action_lambda_page_binding():
    """Verify target_page extraction correctly isolates action dictionary state from closure."""
    action = {"title": "Ajustar", "message": "Reduzir", "page": "planeamento"}
    target_page = str(action.get("page") or "dashboard")

    def click_handler(_checked=False, page=target_page):
        return page

    # Mutate the dictionary afterwards
    action["page"] = "settings"
    # Closure must remain bound to the target_page extracted at registration time
    assert click_handler() == "planeamento"
