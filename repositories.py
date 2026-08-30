from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Game as GameRecord
from models import GameResult as GameResultRecord
from models import Room as RoomRecord
from models import RoomPlayer as RoomPlayerRecord


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def get_room_records(
    session: AsyncSession, room_id: str
) -> tuple[RoomRecord | None, list[RoomPlayerRecord]]:
    room = await session.get(RoomRecord, room_id)
    if room is None:
        return None, []

    result = await session.scalars(
        select(RoomPlayerRecord)
        .where(RoomPlayerRecord.room_id == room_id)
        .order_by(RoomPlayerRecord.joined_at, RoomPlayerRecord.client_id)
    )
    return room, list(result)


async def create_game(session: AsyncSession, room: Any) -> GameRecord:
    db_room = await ensure_room(session, room.room_id, room.status)
    game = GameRecord(room_id=room.room_id, status="PLAYING")
    session.add(game)
    await session.flush()
    db_room.current_game_id = game.id
    room.current_game_id = game.id
    return game


async def get_room_summaries(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(
            RoomRecord.room_id,
            RoomRecord.status,
            RoomPlayerRecord.client_id,
        )
        .outerjoin(
            RoomPlayerRecord,
            (RoomPlayerRecord.room_id == RoomRecord.room_id)
            & RoomPlayerRecord.connected.is_(True),
        )
        .where(RoomRecord.status.in_(("WAITING", "PLAYING")))
        .order_by(RoomRecord.updated_at.desc(), RoomRecord.room_id)
    )

    summaries: dict[str, dict[str, Any]] = {}
    for room_id, status, client_id in result:
        if client_id is None:
            continue
        summary = summaries.setdefault(
            room_id, {"id": room_id, "players": 0, "status": status}
        )
        summary["players"] += 1
    return list(summaries.values())


async def ensure_room(
    session: AsyncSession, room_id: str, default_status: str = "WAITING"
) -> RoomRecord:
    room = await session.get(RoomRecord, room_id)
    if room is None:
        room = RoomRecord(room_id=room_id, status=default_status)
        session.add(room)
        await session.flush()
    return room


async def save_runtime_room(
    session: AsyncSession,
    room: Any,
    connected_client_ids: Iterable[str] | None = None,
) -> RoomRecord:
    db_room = await ensure_room(session, room.room_id, room.status)
    db_room.status = room.status
    db_room.turn_index = room.turn_index
    db_room.last_marked_num = room.last_marked_num
    db_room.deal_state = room.deal_state
    db_room.current_game_id = room.current_game_id

    connected_ids = (
        set(connected_client_ids)
        if connected_client_ids is not None
        else {client_id for client_id, player in room.players.items() if player.ws}
    )
    for player in room.players.values():
        db_player = await session.get(
            RoomPlayerRecord,
            {"room_id": room.room_id, "client_id": player.client_id},
        )
        if db_player is None:
            db_player = RoomPlayerRecord(
                room_id=room.room_id,
                client_id=player.client_id,
                nickname=player.nickname,
                board=list(player.board),
                marked=list(player.marked),
            )
            session.add(db_player)

        db_player.nickname = player.nickname
        db_player.board = list(player.board)
        db_player.marked = list(player.marked)
        db_player.lines = player.lines
        db_player.score = room.scores.get(player.client_id, 0)
        db_player.last_target_id = player.last_target_id
        db_player.connected = player.client_id in connected_ids
        db_player.last_seen_at = utc_now()
        if db_player.connected:
            db_player.left_at = None

    return db_room


async def finish_game(
    session: AsyncSession, room: Any, winners: Iterable[Any]
) -> None:
    if room.current_game_id is None:
        raise RuntimeError("PLAYING 방에 current_game_id가 없습니다.")
    game = await session.get(GameRecord, room.current_game_id)
    if game is None:
        raise RuntimeError(f"게임을 찾을 수 없습니다: {room.current_game_id}")
    game.status = "ENDED"
    game.ended_at = utc_now()

    winner_ids = {winner.client_id for winner in winners}
    for player in room.active_players:
        score_delta = 1 if player.client_id in winner_ids else 0
        db_player = await session.get(
            RoomPlayerRecord,
            {"room_id": room.room_id, "client_id": player.client_id},
        )
        if db_player is not None:
            db_player.score = room.scores.get(player.client_id, 0)
        existing_result = await session.get(
            GameResultRecord,
            {"game_id": room.current_game_id, "client_id": player.client_id},
        )
        if existing_result is None:
            session.add(
                GameResultRecord(
                    game_id=room.current_game_id,
                    client_id=player.client_id,
                    nickname=player.nickname,
                    final_lines=player.lines,
                    won=player.client_id in winner_ids,
                    score_delta=score_delta,
                )
            )


async def mark_player_disconnected(
    session: AsyncSession, room_id: str, client_id: str
) -> None:
    player = await session.get(
        RoomPlayerRecord, {"room_id": room_id, "client_id": client_id}
    )
    if player is not None:
        player.connected = False
        player.last_seen_at = utc_now()


async def mark_player_left(
    session: AsyncSession, room_id: str, client_id: str
) -> None:
    player = await session.get(
        RoomPlayerRecord, {"room_id": room_id, "client_id": client_id}
    )
    if player is not None:
        player.connected = False
        player.left_at = utc_now()
        player.last_seen_at = player.left_at

async def get_departed_player_candidates(
    session: AsyncSession, cutoff: datetime, limit: int = 500
) -> list[tuple[str, str]]:
    """Only explicit departures expire; unexpected disconnects remain resumable."""
    result = await session.execute(
        select(RoomPlayerRecord.room_id, RoomPlayerRecord.client_id)
        .where(
            RoomPlayerRecord.connected.is_(False),
            RoomPlayerRecord.left_at.is_not(None),
            RoomPlayerRecord.left_at <= cutoff,
        )
        .order_by(RoomPlayerRecord.left_at, RoomPlayerRecord.room_id, RoomPlayerRecord.client_id)
        .limit(limit)
    )
    return list(result)


async def delete_departed_players(
    session: AsyncSession,
    room_id: str,
    client_ids: Iterable[str],
    cutoff: datetime,
    protected_ids: Iterable[str] = (),
) -> list[str]:
    """Recheck expiry under a row lock. The caller owns commit and the room lock."""
    candidates = set(client_ids) - set(protected_ids)
    if not candidates:
        return []
    conditions = (
        RoomPlayerRecord.room_id == room_id,
        RoomPlayerRecord.client_id.in_(candidates),
        RoomPlayerRecord.connected.is_(False),
        RoomPlayerRecord.left_at.is_not(None),
        RoomPlayerRecord.left_at <= cutoff,
    )
    expired_ids = list(await session.scalars(
        select(RoomPlayerRecord.client_id).where(*conditions).with_for_update()
    ))
    if expired_ids:
        await session.execute(
            delete(RoomPlayerRecord).where(
                *conditions, RoomPlayerRecord.client_id.in_(expired_ids)
            )
        )
    return expired_ids
