"""Small runtime state objects shared by the Bambu driver modules."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PendingSpool:
    """FilaMan spool waiting for an AMS tray change."""

    spool_id: int
    filament_data: dict
    slot_index: str | None = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    timer: asyncio.Task | None = None
