"""`POST /ask` - issue #108 fills this router in. Stub mounted by the skeleton (#104)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/ask", tags=["ask"])
