import logging
from typing import Dict, List

from fastapi import WebSocket

logger = logging.getLogger("uvicorn.error")


class ConnectionManager:
    """Tracks live dashboard WebSocket connections per tenant and broadcasts order events to them."""

    def __init__(self):
        self.active: Dict[int, List[WebSocket]] = {}

    async def connect(self, tenant_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.setdefault(tenant_id, []).append(websocket)

    def disconnect(self, tenant_id: int, websocket: WebSocket) -> None:
        conns = self.active.get(tenant_id)
        if conns and websocket in conns:
            conns.remove(websocket)
            if not conns:
                self.active.pop(tenant_id, None)

    async def broadcast(self, tenant_id: int, message: dict) -> None:
        conns = list(self.active.get(tenant_id, []))
        dead = []
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(tenant_id, ws)


manager = ConnectionManager()
