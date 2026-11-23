"""Tests for the image upload/download endpoints and agent capabilities."""

import io
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("PIL", reason="Pillow not installed")
pytest.importorskip("slowapi", reason="slowapi not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.core.auth import require_auth  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.routers import agent as agent_router  # noqa: E402
from app.routers import agent_attachments as attachments_router  # noqa: E402


@pytest.fixture(autouse=True)
def _attachment_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("AGENT_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("AGENT_ATTACHMENT_MAX_BYTES", "10485760")
    monkeypatch.setenv("AGENT_ATTACHMENT_MAX_FILES", "4")


def _image():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _user():
    return SimpleNamespace(id=uuid.uuid4())


@pytest.fixture
def client():
    current = {"user": _user()}
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(agent_router.router)
    app.include_router(attachments_router.router)
    app.dependency_overrides[require_auth] = lambda: current["user"]
    client = TestClient(app, raise_server_exceptions=False)
    client.current_user = current  # type: ignore[attr-defined]
    return client


def test_upload_and_download_round_trip(client):
    response = client.post(
        "/agent/attachments",
        files={"file": ("photo.png", _image(), "image/png")},
    )
    assert response.status_code == 200, response.text
    attachment = response.json()
    assert attachment["name"] == "photo.png"
    assert attachment["mime_type"] == "image/jpeg"
    assert attachment["width"] == 32 and attachment["height"] == 24

    download = client.get(f"/agent/attachments/{attachment['id']}")
    assert download.status_code == 200
    assert download.headers["content-type"] == "image/jpeg"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.content.startswith(b"\xff\xd8\xff")


def test_foreign_user_cannot_download(client):
    response = client.post(
        "/agent/attachments",
        files={"file": ("photo.png", _image(), "image/png")},
    )
    attachment_id = response.json()["id"]
    client.current_user["user"] = _user()

    download = client.get(f"/agent/attachments/{attachment_id}")
    assert download.status_code == 404


def test_rejects_oversized_upload(client, monkeypatch):
    monkeypatch.setenv("AGENT_ATTACHMENT_MAX_BYTES", "10")
    response = client.post(
        "/agent/attachments",
        files={"file": ("photo.png", _image(), "image/png")},
    )
    assert response.status_code == 413


def test_capabilities_follow_vision_configuration(client, monkeypatch):
    monkeypatch.setattr(
        agent_router,
        "load_config",
        lambda: SimpleNamespace(url=None, mock=False),
    )
    monkeypatch.setattr(agent_router, "vision_configured", lambda: True)
    response = client.get("/agent/capabilities")
    assert response.status_code == 200
    assert response.json()["image_input"] is True
    assert response.json()["max_images"] == 4

    monkeypatch.setattr(agent_router, "vision_configured", lambda: False)
    response = client.get("/agent/capabilities")
    assert response.json()["image_input"] is False
