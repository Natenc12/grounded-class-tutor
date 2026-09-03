"""`POST /files` + `GET /files/{file_id}` - issue #110 fills this router in. Stub (#104)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/files", tags=["files"])
