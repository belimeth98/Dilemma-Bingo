"""Server-owned cleanup of explicitly departed room participants, never game history."""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from database import get_session_factory
from models import Room as RoomRecord
from repositories import delete_departed_players, get_departed_player_candidates

logger = logging.getLogger(__name__)
RETENTION = timedelta(hours=24)
CLEANUP_INTERVAL_SECONDS = 10 * 60
CLEANUP_BATCH_SIZE = 500


def pending_participants(room) -> set[str]:
    if room is None or room.status != "PLAYING" or not room.deal_state:
        return set()
    return {
        value for key in ("initiator", "target", "bonus_player")
        if isinstance(value := room.deal_state.get(key), str)
    }


async def cleanup_departed_players(manager, *, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Cleanup time must include a timezone")
    cutoff = now.astimezone(timezone.utc) - RETENTION
    async with get_session_factory()() as session:
        candidates = await get_departed_player_candidates(session, cutoff, CLEANUP_BATCH_SIZE)
    by_room = defaultdict(list)
    for room_id, client_id in candidates:
        by_room[room_id].append(client_id)

    total = 0
    for room_id, client_ids in by_room.items():
        try:
            # Same lock as joins/actions: reconnect cannot race the deletion or
            # recreate a deleted row from stale runtime state during a game save.
            async with manager.get_room_lock(room_id):
                room = manager.rooms.get(room_id)
                protected = pending_participants(room)
                if room is not None:
                    protected.update(p.client_id for p in room.players.values() if p.ws is not None)
                async with get_session_factory()() as session:
                    try:
                        db_room = await session.get(RoomRecord, room_id)
                        protected.update(pending_participants(db_room))
                        deleted = await delete_departed_players(
                            session, room_id, client_ids, cutoff, protected
                        )
                        if deleted:
                            await session.commit()
                    except Exception:
                        await session.rollback()
                        raise
                # Update only expired participants after the database committed.
                # Active order, turn index, deal, timer and historical results stay intact.
                if room is not None:
                    for client_id in deleted:
                        room.players.pop(client_id, None)
                        room.scores.pop(client_id, None)
                        if client_id in room.player_order:
                            room.player_order.remove(client_id)
                total += len(deleted)
        except Exception as error:
            # A failing room must not stop cleanup of unrelated rooms, or leak DB secrets.
            logger.warning("Departed-player cleanup failed (%s)", type(error).__name__)
    return total


async def run_player_cleanup(manager, *, interval: float = CLEANUP_INTERVAL_SECONDS) -> None:
    """Run once at startup, then periodically; cancellation is owned by lifespan."""
    if interval <= 0:
        raise ValueError("Cleanup interval must be positive")
    while True:
        try:
            deleted = await cleanup_departed_players(manager)
            if deleted:
                logger.info("Removed %d expired room participants", deleted)
        except Exception as error:
            logger.warning("Departed-player cleanup unavailable (%s)", type(error).__name__)
        await asyncio.sleep(interval)
