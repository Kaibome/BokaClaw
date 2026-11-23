"""Tests for local image attachment storage and model content-part assembly."""

import io
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("PIL", reason="Pillow not installed")

from PIL import Image  # noqa: E402

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent import attachments  # noqa: E402
from app.agent.events import AgentLoopError  # noqa: E402
from app.agent.runtime import build_messages  # noqa: E402
from app.agent.schemas import AgentInvokeRequest  # noqa: E402
from app.agent.store import build_history  # noqa: E402


@pytest.fixture(autouse=True)
def _attachment_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("AGENT_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("AGENT_HISTORY_MAX_IMAGES", "4")
    monkeypatch.setenv("AGENT_ATTACHMENT_MAX_FILES", "4")
    monkeypatch.setenv("AGENT_ATTACHMENT_MAX_TOTAL_BYTES", "6291456")


def _image(color=(20, 120, 200), size=(200, 100), fmt="PNG", exif_text=None):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    if exif_text:
        exif = image.getexif()
        exif[0x010E] = exif_text
        image.save(buffer, format=fmt, exif=exif)
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


def _upload(owner=None, data=None, name="photo.png"):
    return attachments.store_upload(
        owner or str(uuid.uuid4()),
        name,
        data if data is not None else _image(),
    )


def test_store_upload_normalizes_and_deduplicates(tmp_path):
    owner = str(uuid.uuid4())
    first = _upload(owner, _image(size=(2000, 1000)), name="../../evil name.png")
    second = _upload(owner, _image(size=(2000, 1000)), name="another.png")

    assert first["id"] == second["id"]
    assert first["mime_type"] == "image/jpeg"
    assert first["width"] == 1568
    assert first["height"] == 784
    assert first["name"] == "evil name.png"
    assert attachments.load_bytes(owner, first["id"])

    stored = tmp_path / "attachments" / owner / first["id"]
    sidecar = tmp_path / "attachments" / owner / first["id"].replace(".jpg", ".json")
    assert stored.is_file() and sidecar.is_file()
    assert (stored.stat().st_mode & 0o777) == 0o600


def test_store_upload_strips_exif():
    owner = str(uuid.uuid4())
    descriptor = _upload(owner, _image(fmt="JPEG", exif_text="secret gps"), name="p.jpg")

    with Image.open(io.BytesIO(attachments.load_bytes(owner, descriptor["id"]))) as stored:
        assert not stored.getexif()


def test_store_upload_rejects_non_images_and_oversized_files(monkeypatch):
    owner = str(uuid.uuid4())
    with pytest.raises(attachments.AttachmentError) as error:
        _upload(owner, b"not an image", name="x.txt")
    assert error.value.code == "attachment_invalid_type"

    monkeypatch.setenv("AGENT_ATTACHMENT_MAX_BYTES", "10")
    with pytest.raises(attachments.AttachmentError) as error:
        _upload(owner, _image(), name="large.png")
    assert error.value.status_code == 413


def test_attachment_ids_reject_traversal_and_foreign_users():
    owner = str(uuid.uuid4())
    descriptor = _upload(owner, name="safe.png")

    assert attachments.valid_attachment_id(descriptor["id"])
    assert not attachments.valid_attachment_id("../" + descriptor["id"])
    assert attachments.load_bytes(str(uuid.uuid4()), descriptor["id"]) is None
    assert attachments.load_descriptor(str(uuid.uuid4()), descriptor["id"]) is None


def test_validate_for_send_deduplicates_and_enforces_total(monkeypatch):
    owner = str(uuid.uuid4())
    descriptor = _upload(owner)
    assert attachments.validate_for_send(owner, [descriptor["id"], descriptor["id"]]) == [descriptor]

    monkeypatch.setenv("AGENT_ATTACHMENT_MAX_TOTAL_BYTES", "1")
    with pytest.raises(attachments.AttachmentError) as error:
        attachments.validate_for_send(owner, [descriptor["id"]])
    assert error.value.code == "attachment_total_too_large"


def test_prune_orphans_deletes_unreferenced_files():
    owner = str(uuid.uuid4())
    descriptor = _upload(owner)
    root = attachments.attachments_root()
    old = time.time() - 3 * 24 * 60 * 60
    for path in (root / owner).iterdir():
        os.utime(path, (old, old))

    class FakeDatabase:
        def __init__(self, referenced):
            self.referenced = referenced

        def execute(self, _statement):
            return [(value,) for value in self.referenced]

    stats = attachments.prune_orphans(FakeDatabase(set()))
    assert stats["deleted"] >= 2
    assert not (root / owner / descriptor["id"]).exists()

    referenced_descriptor = _upload(owner)
    for path in (root / owner).iterdir():
        os.utime(path, (old, old))
    stats = attachments.prune_orphans(FakeDatabase({referenced_descriptor["id"]}))
    assert stats["kept"] >= 1
    assert (root / owner / referenced_descriptor["id"]).exists()


def test_build_messages_emits_image_parts_and_keeps_text_plain():
    owner = str(uuid.uuid4())
    descriptor = _upload(owner)
    payload = {
        "message": "what is this?",
        "user_id": owner,
        "attachments": [descriptor],
        "attachment_ids": [descriptor["id"]],
        "history": [],
    }

    messages = build_messages(payload)
    assert [part["type"] for part in messages[0]["content"]] == ["text", "image_url"]
    assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "base64" not in json.dumps({**payload, "attachments": []})

    plain = build_messages({"message": "hello", "user_id": owner, "history": []})
    assert plain[0]["content"] == "hello"
    assert isinstance(plain[0]["content"], str)


def test_build_messages_expands_history_images_and_tolerates_missing_ones():
    owner = str(uuid.uuid4())
    descriptor = _upload(owner)
    messages = build_messages(
        {
            "message": "follow up",
            "user_id": owner,
            "history": [
                {
                    "role": "user",
                    "content": "earlier",
                    "attachments": [descriptor],
                }
            ],
        }
    )

    assert [part["type"] for part in messages[0]["content"]] == ["text", "image_url"]
    assert messages[1] == {"role": "user", "content": "follow up"}

    (attachments.attachments_root() / owner / descriptor["id"]).unlink()
    messages = build_messages(
        {
            "message": "follow up",
            "user_id": owner,
            "history": [
                {
                    "role": "user",
                    "content": "earlier",
                    "attachments": [descriptor],
                }
            ],
        }
    )
    assert messages[0]["content"] == [
        {"type": "text", "text": "earlier"},
        {"type": "text", "text": "[Image unavailable]"},
    ]


def test_build_messages_rejects_a_missing_current_turn_image():
    owner = str(uuid.uuid4())
    descriptor = _upload(owner)
    (attachments.attachments_root() / owner / descriptor["id"]).unlink()

    with pytest.raises(AgentLoopError) as error:
        build_messages(
            {
                "message": "look",
                "user_id": owner,
                "attachments": [descriptor],
                "attachment_ids": [descriptor["id"]],
                "history": [],
            }
        )
    assert error.value.code == "agent_attachment_missing"


def _message(role, content, *, attachments=None):
    metadata = {"attachments": attachments} if attachments else {}
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        content=content,
        status="completed",
        meta=metadata,
        created_at=datetime.now(timezone.utc),
    )


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self, _statement):
        return _Rows(self.rows)


def test_build_history_keeps_newest_images_and_marks_older_ones(monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_MAX_IMAGES", "1")
    first = {
        "id": "a" * 64 + ".jpg",
        "name": "a.jpg",
        "mime_type": "image/jpeg",
        "bytes": 10,
        "width": 1,
        "height": 1,
    }
    second = {**first, "id": "b" * 64 + ".jpg", "name": "b.jpg"}
    # Newest first: the image in this user turn wins the single image slot.
    rows = [
        _message("assistant", "answer"),
        _message("user", "newest", attachments=[second]),
        _message("assistant", "older answer"),
        _message("user", "", attachments=[first]),
    ]

    history = build_history(_Session(rows), uuid.uuid4(), limit=40)
    rendered = [item for item in history if item["role"] == "user"]
    assert rendered[0]["content"] == "[Earlier image omitted]"
    assert "attachments" not in rendered[0]
    assert rendered[1]["content"] == "newest"
    assert rendered[1]["attachments"][0]["id"] == second["id"]


def test_agent_invoke_request_allows_images_without_caption():
    request = AgentInvokeRequest(attachment_ids=["a" * 64 + ".jpg"])
    assert request.message == ""
    assert request.attachment_ids

    with pytest.raises(ValueError):
        AgentInvokeRequest()
