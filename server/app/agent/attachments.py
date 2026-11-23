"""Local, content-addressed storage for user-uploaded images.

The server keeps attachment metadata in ``conversation_messages.metadata``;
the bytes live under ``AGENT_ATTACHMENTS_ROOT`` in a per-user directory. The
attachment id is the SHA-256 of the normalized bytes plus the normalized
extension, so re-uploading the same image is idempotent and no database row is
needed. A small JSON sidecar next to each image keeps the display name and
dimensions that content addressing would otherwise lose.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT_ATTACHMENTS_ROOT = "/app/history/attachments"
DEFAULT_WORKSPACE_ROOT = "/app/workspace"
DEFAULT_MEMORY_ROOT = "/app/memory"
DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_NORMALIZED_BYTES = 3 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 6 * 1024 * 1024
DEFAULT_MAX_FILES = 4
DEFAULT_MAX_EDGE = 1568
DEFAULT_PRUNE_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_HISTORY_MAX_IMAGES = 4
DEFAULT_IMAGE_TOKEN_ESTIMATE = 1200
ALLOWED_MIME_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{64}\.(jpg|png)$")
MAX_IMAGE_PIXELS = 40_000_000
JPEG_QUALITIES = (88, 78, 68)


class AttachmentError(Exception):
    """A user-correctable upload / attachment validation failure."""

    def __init__(self, code: str, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def attachments_root() -> Path:
    raw = (os.environ.get("AGENT_ATTACHMENTS_ROOT") or DEFAULT_ATTACHMENTS_ROOT).strip()
    return Path(raw).expanduser()


def workspace_root() -> Path:
    raw = (os.environ.get("AGENT_WORKSPACE_ROOT") or DEFAULT_WORKSPACE_ROOT).strip()
    return Path(raw).expanduser()


def memory_root() -> Path:
    raw = (os.environ.get("AGENT_MEMORY_ROOT") or DEFAULT_MEMORY_ROOT).strip()
    return Path(raw).expanduser()


def max_upload_bytes() -> int:
    return _env_int("AGENT_ATTACHMENT_MAX_BYTES", DEFAULT_MAX_UPLOAD_BYTES, minimum=1)


def max_normalized_bytes() -> int:
    return _env_int("AGENT_ATTACHMENT_MAX_NORMALIZED_BYTES", DEFAULT_MAX_NORMALIZED_BYTES, minimum=1)


def max_total_bytes() -> int:
    return _env_int("AGENT_ATTACHMENT_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES, minimum=1)


def max_files() -> int:
    return _env_int("AGENT_ATTACHMENT_MAX_FILES", DEFAULT_MAX_FILES, minimum=1)


def max_edge() -> int:
    return _env_int("AGENT_ATTACHMENT_MAX_EDGE", DEFAULT_MAX_EDGE, minimum=64)


def prune_interval_seconds() -> int:
    return _env_int("AGENT_ATTACHMENT_PRUNE_INTERVAL_SECONDS", DEFAULT_PRUNE_INTERVAL_SECONDS, minimum=0)


def history_max_images() -> int:
    return _env_int("AGENT_HISTORY_MAX_IMAGES", DEFAULT_HISTORY_MAX_IMAGES, minimum=0)


def image_token_estimate() -> int:
    return _env_int("AGENT_IMAGE_TOKEN_ESTIMATE", DEFAULT_IMAGE_TOKEN_ESTIMATE, minimum=1)


def _resolved(path: Path) -> Path:
    return path.resolve(strict=False)


def root_is_safe(root: Path | None = None) -> bool:
    """Reject roots inside the agent workspace or the authoritative memory tree."""

    candidate = _resolved(root or attachments_root())
    for forbidden in (_resolved(workspace_root()), _resolved(memory_root())):
        if candidate == forbidden:
            return False
        if candidate.is_relative_to(forbidden):
            return False
        # Also refuse a root that contains the workspace/memory tree: the prune
        # sweep must never be able to walk outside a dedicated attachments dir.
        if forbidden.is_relative_to(candidate):
            return False
    return True


def ensure_root() -> Path:
    root = attachments_root()
    if not root_is_safe(root):
        raise AttachmentError(
            "attachment_storage_misconfigured",
            "Attachment storage must not be inside the agent workspace or memory root.",
            status_code=500,
        )
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise AttachmentError(
            "attachment_storage_unavailable",
            f"Attachment storage is unavailable: {exc}",
            503,
        ) from exc
    if not os.access(root, os.W_OK):
        raise AttachmentError("attachment_storage_unavailable", "Attachment storage is not writable.", 503)
    return root


def upload_available() -> bool:
    try:
        ensure_root()
    except AttachmentError:
        return False
    return True


def user_dir(user_id: str) -> Path:
    try:
        owner = str(uuid.UUID(str(user_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AttachmentError("attachment_invalid_user", "Invalid attachment owner.", 400) from exc
    root = ensure_root()
    directory = root / owner
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise AttachmentError(
            "attachment_storage_unavailable",
            f"Attachment storage is unavailable: {exc}",
            503,
        ) from exc
    return directory


def valid_attachment_id(value: str) -> bool:
    return isinstance(value, str) and bool(ATTACHMENT_ID_RE.match(value))


def _clean_name(filename: str | None) -> str:
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n\t")
    name = name.strip(" .")
    if not name:
        return "image"
    return name[:120]


def _detect_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _open_image(data: bytes):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        image = Image.open(io.BytesIO(data))
        image.seek(0)
        image.load()
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise AttachmentError("attachment_invalid_image", "The image dimensions are too large.", 413)
        return ImageOps.exif_transpose(image), Image
    except AttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow raises several types for corrupt input
        raise AttachmentError("attachment_invalid_image", "The uploaded file is not a readable image.", 400) from exc


def _save_image(image: Any, image_module: Any) -> tuple[bytes, str, str]:
    """Return normalized bytes, mime type, and extension."""

    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha:
        converted = image.convert("RGBA")
        buffer = io.BytesIO()
        converted.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), "image/png", "png"

    converted = image.convert("RGB")
    limit = max_normalized_bytes()
    data = b""
    for quality in JPEG_QUALITIES:
        buffer = io.BytesIO()
        converted.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= limit:
            break
    return data, "image/jpeg", "jpg"


def normalize_image(data: bytes) -> tuple[bytes, str, int, int]:
    """Validate, strip metadata, downscale, and re-encode one image."""

    mime = _detect_mime(data)
    if mime is None or mime not in ALLOWED_MIME_TYPES:
        raise AttachmentError(
            "attachment_invalid_type",
            "Only JPEG, PNG, WebP, and GIF images are supported.",
            400,
        )

    image, image_module = _open_image(data)
    try:
        edge = max_edge()
        if image.width > edge or image.height > edge:
            image.thumbnail((edge, edge), image_module.Resampling.LANCZOS)

        normalized, output_mime, extension = _save_image(image, image_module)
        if len(normalized) > max_normalized_bytes():
            # Large transparent PNGs: shrink until they fit.
            attempts = 0
            while len(normalized) > max_normalized_bytes() and attempts < 4:
                attempts += 1
                width = max(1, int(image.width * 0.8))
                height = max(1, int(image.height * 0.8))
                image = image.resize((width, height), image_module.Resampling.LANCZOS)
                normalized, output_mime, extension = _save_image(image, image_module)
        if len(normalized) > max_normalized_bytes():
            raise AttachmentError("attachment_too_large", "The image is too large after processing.", 413)
        return normalized, output_mime, image.width, image.height
    finally:
        image.close()


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _descriptor_from_payload(attachment_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return {
            "id": attachment_id,
            "name": _clean_name(str(payload.get("name") or "image")),
            "mime_type": str(payload.get("mime_type") or "image/jpeg"),
            "bytes": int(payload.get("bytes") or 0),
            "width": int(payload.get("width") or 0),
            "height": int(payload.get("height") or 0),
        }
    except (TypeError, ValueError):
        return None


def store_upload(user_id: str, filename: str | None, data: bytes) -> dict[str, Any]:
    if len(data) > max_upload_bytes():
        raise AttachmentError("attachment_too_large", "The uploaded image exceeds the size limit.", 413)

    normalized, mime_type, width, height = normalize_image(data)
    digest = hashlib.sha256(normalized).hexdigest()
    extension = "png" if mime_type == "image/png" else "jpg"
    attachment_id = f"{digest}.{extension}"
    directory = user_dir(user_id)
    target = directory / attachment_id
    sidecar = directory / f"{digest}.json"

    if not target.exists():
        _atomic_write(target, normalized)
    elif target.stat().st_size != len(normalized):
        # A hash collision is practically impossible; replace a truncated file.
        _atomic_write(target, normalized)

    descriptor: dict[str, Any] | None = None
    if sidecar.exists():
        try:
            descriptor = _descriptor_from_payload(attachment_id, json.loads(sidecar.read_text("utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            descriptor = None
    if descriptor is None:
        descriptor = {
            "id": attachment_id,
            "name": _clean_name(filename),
            "mime_type": mime_type,
            "bytes": len(normalized),
            "width": width,
            "height": height,
        }
        _atomic_write_json(
            sidecar,
            {
                "name": descriptor["name"],
                "mime_type": mime_type,
                "bytes": len(normalized),
                "width": width,
                "height": height,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    return descriptor


def load_descriptor(user_id: str, attachment_id: str) -> dict[str, Any] | None:
    if not valid_attachment_id(attachment_id):
        return None
    try:
        directory = user_dir(user_id)
    except AttachmentError:
        return None
    target = directory / attachment_id
    if not target.is_file():
        return None
    digest = attachment_id.rsplit(".", 1)[0]
    sidecar = directory / f"{digest}.json"
    if sidecar.is_file():
        try:
            descriptor = _descriptor_from_payload(attachment_id, json.loads(sidecar.read_text("utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            descriptor = None
        if descriptor is not None:
            return descriptor
    stat = target.stat()
    return {
        "id": attachment_id,
        "name": attachment_id,
        "mime_type": "image/png" if attachment_id.endswith(".png") else "image/jpeg",
        "bytes": stat.st_size,
        "width": 0,
        "height": 0,
    }


def load_bytes(user_id: str, attachment_id: str) -> bytes | None:
    if not valid_attachment_id(attachment_id):
        return None
    try:
        directory = user_dir(user_id)
    except AttachmentError:
        return None
    target = directory / attachment_id
    try:
        return target.read_bytes() if target.is_file() else None
    except OSError:
        return None


def validate_for_send(user_id: str, attachment_ids: Iterable[str]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in attachment_ids:
        if value in seen:
            continue
        if not valid_attachment_id(value):
            raise AttachmentError("attachment_invalid_id", "Invalid attachment id.", 400)
        seen.add(value)
        unique.append(value)

    limit = max_files()
    if len(unique) > limit:
        raise AttachmentError("attachment_too_many", f"At most {limit} images can be sent per turn.", 400)

    descriptors: list[dict[str, Any]] = []
    total = 0
    for attachment_id in unique:
        descriptor = load_descriptor(user_id, attachment_id)
        if descriptor is None:
            raise AttachmentError("attachment_not_found", "The attachment is missing or expired.", 400)
        total += int(descriptor.get("bytes") or 0)
        descriptors.append(descriptor)
    if total > max_total_bytes():
        raise AttachmentError("attachment_total_too_large", "The selected images exceed the total size limit.", 413)
    return descriptors


def data_url(mime_type: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def image_content_part(descriptor: dict[str, Any], data: bytes, *, detail: str | None = None) -> dict[str, Any]:
    image_url: dict[str, Any] = {"url": data_url(str(descriptor.get("mime_type") or "image/jpeg"), data)}
    if detail:
        image_url["detail"] = detail
    return {"type": "image_url", "image_url": image_url}


def referenced_attachment_ids(db: Any) -> set[str]:
    """Collect ids referenced by any persisted user message."""

    from sqlalchemy import text

    rows = db.execute(
        text(
            """
            SELECT DISTINCT element->>'id'
            FROM conversation_messages,
                 jsonb_array_elements(metadata->'attachments') AS element
            WHERE metadata ? 'attachments'
              AND jsonb_typeof(metadata->'attachments') = 'array'
            """
        )
    )
    return {str(row[0]) for row in rows if row and row[0]}


def prune_orphans(db: Any, *, older_than_seconds: float = 24 * 60 * 60) -> dict[str, int]:
    """Delete files that no message references and that are old enough."""

    root = attachments_root()
    if not root_is_safe(root):
        logger.warning("Refusing to prune attachments from an unsafe root: %s", root)
        return {"deleted": 0, "kept": 0}
    if not root.is_dir():
        return {"deleted": 0, "kept": 0}
    referenced = referenced_attachment_ids(db)
    cutoff = time.time() - max(0.0, older_than_seconds)
    deleted = 0
    kept = 0
    for user_entry in root.iterdir():
        if not user_entry.is_dir():
            continue
        for path in user_entry.iterdir():
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix not in {".jpg", ".png", ".json"}:
                continue
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                # A sidecar removed earlier in this same pass is not an orphan.
                continue
            if mtime > cutoff:
                kept += 1
                continue
            if path.suffix == ".json":
                jpg_name = f"{path.stem}.jpg"
                png_name = f"{path.stem}.png"
                if jpg_name in referenced or png_name in referenced:
                    kept += 1
                    continue
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    logger.warning("Could not prune attachment sidecar %s", path, exc_info=True)
                continue
            if path.name in referenced:
                kept += 1
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError:
                logger.warning("Could not prune attachment %s", path, exc_info=True)
            sidecar = path.parent / f"{path.stem}.json"
            try:
                if sidecar.exists():
                    sidecar.unlink()
                    deleted += 1
            except OSError:
                logger.warning("Could not prune attachment sidecar %s", sidecar, exc_info=True)
    return {"deleted": deleted, "kept": kept}
