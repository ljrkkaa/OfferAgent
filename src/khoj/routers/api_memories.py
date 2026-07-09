import json
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.authentication import requires

from khoj.processor.conversation.offeragent_memory import (
    delete_memory as delete_offeragent_memory,
)
from khoj.processor.conversation.offeragent_memory import (
    get_memory_by_id,
    list_memories,
)
from khoj.processor.conversation.offeragent_memory import (
    update_memory as update_offeragent_memory,
)

api_memories = APIRouter()
logger = logging.getLogger(__name__)


@api_memories.get("")
@requires(["authenticated"])
async def get_memories(
    request: Request,
    client: Optional[str] = None,
):
    """Get all memories for the authenticated user"""
    formatted_memories = [
        {
            "id": memory.id,
            "raw": memory.raw,
            "type": memory.memory_type,
            "description": memory.description,
            "created_at": memory.created_at.isoformat(),
            "updated_at": memory.updated_at.isoformat(),
        }
        for memory in list_memories()
    ]

    return Response(content=json.dumps(formatted_memories), media_type="application/json", status_code=200)


@api_memories.delete("/{memory_id}")
@requires(["authenticated"])
async def delete_memory(
    request: Request,
    memory_id: str,
    client: Optional[str] = None,
):
    """Delete a specific memory by ID"""
    try:
        deleted = delete_offeragent_memory(memory_id)
    except ValueError:
        deleted = False
    if not deleted:
        return Response(
            content=json.dumps({"error": "Memory not found"}), media_type="application/json", status_code=404
        )

    return Response(status_code=204)


class UpdateMemoryBody(BaseModel):
    """Request model for updating a memory"""

    raw: str


@api_memories.put("/{memory_id}")
@requires(["authenticated"])
async def update_memory(
    request: Request,
    body: UpdateMemoryBody,
    memory_id: str,
    client: Optional[str] = None,
):
    """Update a specific memory's content"""
    try:
        memory = get_memory_by_id(memory_id)
    except ValueError:
        memory = None
    if not memory:
        return Response(
            content=json.dumps({"error": "Memory not found"}), media_type="application/json", status_code=404
        )

    new_content = body.raw
    if not new_content:
        return Response(
            content=json.dumps({"error": "Missing required field 'raw'"}),
            media_type="application/json",
            status_code=400,
        )

    try:
        memory = update_offeragent_memory(memory_id, new_content)
    except ValueError as e:
        return Response(content=json.dumps({"error": str(e)}), media_type="application/json", status_code=400)

    return Response(
        content=json.dumps(
            {
                "id": memory.id,
                "raw": memory.raw,
                "type": memory.memory_type,
                "description": memory.description,
                "created_at": memory.created_at.isoformat(),
                "updated_at": memory.updated_at.isoformat(),
            }
        ),
        media_type="application/json",
        status_code=200,
    )
