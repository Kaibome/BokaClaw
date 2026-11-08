from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_admin, verify_auth
from app.core.errors import upstream_error
from app.core.server_state import get_memory_instance
from app.schemas import MessageResponse

router = APIRouter(prefix="/entities", tags=["entities"])

SCAN_LIMIT = 10_000

EntityType = Literal["user", "agent", "run"]
TYPE_TO_FIELD: dict[EntityType, str] = {"user": "user_id", "agent": "agent_id", "run": "run_id"}


class Entity(BaseModel):
    id: str
    type: EntityType
    total_memories: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("", response_model=list[Entity])
def list_entities(_auth=Depends(verify_auth)):
    rows = get_memory_instance().repository.entities(limit=SCAN_LIMIT)
    return [
        Entity(
            id=str(row["id"]),
            type=row["type"],
            total_memories=int(row["total_memories"]),
            created_at=_parse_timestamp(row.get("created_at")),
            updated_at=_parse_timestamp(row.get("updated_at")),
        )
        for row in rows
    ]


@router.delete("/{entity_type}/{entity_id}", response_model=MessageResponse)
def delete_entity(entity_type: EntityType, entity_id: str, _auth=Depends(require_admin)):
    try:
        get_memory_instance().delete_all(**{TYPE_TO_FIELD[entity_type]: entity_id})
    except Exception:
        raise upstream_error()
    return MessageResponse(message="Entity deleted")
