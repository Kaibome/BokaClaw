"""Upload and download endpoints for user-attached images."""

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile

from app.agent import attachments
from app.agent.schemas import AttachmentOut
from app.core.auth import require_auth
from app.core.rate_limit import limiter
from app.models import User

router = APIRouter(prefix="/agent/attachments", tags=["agent"])


def _raise(exc: attachments.AttachmentError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("", response_model=AttachmentOut, summary="Upload one image attachment")
@limiter.limit("30/minute")
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_auth),
):
    if not attachments.upload_available():
        raise HTTPException(status_code=503, detail="Attachment storage is not available.")
    limit = attachments.max_upload_bytes()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="The uploaded image exceeds the size limit.")
    try:
        descriptor = await asyncio.to_thread(attachments.store_upload, str(user.id), file.filename, data)
    except attachments.AttachmentError as exc:
        _raise(exc)
    return AttachmentOut(**descriptor)


@router.get("/{attachment_id}", summary="Download one image attachment")
async def download_attachment(
    attachment_id: str,
    user: User = Depends(require_auth),
):
    descriptor = attachments.load_descriptor(str(user.id), attachment_id)
    data = attachments.load_bytes(str(user.id), attachment_id)
    if descriptor is None or data is None:
        # Missing and foreign attachments are indistinguishable.
        raise HTTPException(status_code=404, detail="Attachment not found.")
    name = quote(str(descriptor.get("name") or "image"), safe="")
    headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "ETag": f'"{attachment_id.rsplit(".", 1)[0]}"',
        "Content-Disposition": f"inline; filename*=UTF-8''{name}",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=data, media_type=str(descriptor.get("mime_type") or "image/jpeg"), headers=headers)
