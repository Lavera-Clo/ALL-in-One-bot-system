"""
Test setup: the app reads its database URL and keys when it is first imported, so these
are pointed at a throwaway database and blanked BEFORE anything imports `main`.
"""
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)  # main.py mounts ./static relative to the working directory

_db_dir = tempfile.mkdtemp(prefix="botplatform-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{Path(_db_dir, 'test.db').as_posix()}"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test-password"
os.environ["META_VERIFY_TOKEN"] = "test-verify-token"
# No real keys or tokens in tests: WhatsApp runs in mock mode and the bot uses its keyword fallback.
for name in ("META_ACCESS_TOKEN", "META_APP_SECRET", "GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
             "OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2"):
    os.environ[name] = ""
os.environ["OPENAI_API_KEY"] = "sk-placeholder"
os.environ["STRIPE_SECRET_KEY"] = "sk_placeholder"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    import main

    # The rate limiter counts per IP; tests fire far more requests than one real client would
    main.app.user_middleware = [m for m in main.app.user_middleware if m.cls.__name__ != "RateLimitMiddleware"]
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def auth(client):
    """Authorization header for the bootstrapped owner account."""
    res = client.post("/api/admin/login", json={"username": "admin", "password": "test-password"})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


@pytest.fixture
def outbox(monkeypatch):
    """Records every WhatsApp message the bot tries to send, instead of calling Meta."""
    import main
    import order_service

    sent = []

    def recorder(kind):
        async def fake_send(*args, **kwargs):
            sent.append({"kind": kind, **kwargs})
            return {"status": "recorded"}
        return fake_send

    for module in (main, order_service):
        for name, kind in [
            ("send_whatsapp_message", "text"),
            ("send_whatsapp_button_message", "buttons"),
            ("send_whatsapp_list_message", "list"),
            ("send_whatsapp_document", "document"),
            ("notify_human_agent", "handoff"),
            ("send_whatsapp_typing_indicator", "typing"),
            ("send_whatsapp_voice_note", "voice"),
        ]:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, recorder(kind))
    return sent
