"""Read-only spectator views. Never join a room or forward participant payloads."""

import asyncio
import json
import logging
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import and_, func, or_, select

from database import get_session_factory
from models import Game, GameResult, Room, RoomPlayer

logger = logging.getLogger(__name__)
PAGE_SIZES = (1, 2, 4)
LOG_PAGE_SIZE = 4
LOG_LIMIT = 200
GAME_LOG_LIMIT = 256
MAX_WATCHED_GAMES = 2048


def public_player(player, connected: bool, public_id: str) -> dict:
    # Explicit allowlist: Player.to_dict() also contains private marked numbers.
    return {
        "id": public_id,
        "nickname": player.nickname,
        "lines": player.lines,
        "connected": connected,
        "winning_nums": sorted(player.get_winning_numbers()),
    }


def public_deal(state, names: dict[str, str]) -> dict | None:
    if not state:
        return None
    return {
        "phase": state.get("phase", "CHOICE"),
        "initiator_name": names.get(state.get("initiator"), "연결 대기 중"),
        "target_name": names.get(state.get("target"), "연결 대기 중"),
        "bonus_name": names.get(state.get("bonus_player")),
    }


def parse_subscription(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("조회 형식이 올바르지 않습니다.")
    page, size = payload.get("page", 1), payload.get("page_size", 4)
    log_pages = payload.get("log_pages", {})
    if type(page) is not int or not 1 <= page <= 1_000_000:
        raise ValueError("페이지가 올바르지 않습니다.")
    if type(size) is not int or size not in PAGE_SIZES:
        raise ValueError("화면 수는 1, 2, 4 중 선택하세요.")
    if not isinstance(log_pages, dict) or len(log_pages) > 4:
        raise ValueError("로그는 최대 4개 게임까지 조회할 수 있습니다.")
    if any(
        not isinstance(key, str) or not key.isdecimal() or len(key) > 20
        or type(value) is not int or not 1 <= value <= LOG_LIMIT // LOG_PAGE_SIZE
        for key, value in log_pages.items()
    ):
        raise ValueError("로그 페이지가 올바르지 않습니다.")
    watched_game_ids = payload.get("watched_game_ids", [])
    if (not isinstance(watched_game_ids, list) or len(watched_game_ids) > MAX_WATCHED_GAMES
            or any(type(value) is not int or not 1 <= value <= 2_147_483_647 for value in watched_game_ids)):
        raise ValueError("관전 게임 목록이 올바르지 않습니다.")
    request_id = payload.get("request_id", 0)
    if type(request_id) is not int or not 0 <= request_id <= 1_000_000_000:
        raise ValueError("조회 번호가 올바르지 않습니다.")
    return {"page": page, "page_size": size, "log_pages": log_pages, "request_id": request_id, "watched_game_ids": list(dict.fromkeys(watched_game_ids))}


class PublicGameStatus:
    def __init__(self, manager):
        self.manager = manager
        self.logs = OrderedDict()
        self.sequence = 0
        self.trades = {}

    def record_event(self, room, message: dict) -> None:
        """Only already-public logs, called after the existing transaction commits.

        No I/O, tasks, participant sends, timers or game mutations are introduced.
        Memory is bounded, including abandoned rooms and repeated games.
        """
        if room is None or room.current_game_id is None:
            return
        if message.get("type") != "event_log" or not isinstance(message.get("message"), str):
            return
        game_id = room.current_game_id
        if game_id not in self.logs:
            self.logs[game_id] = deque(maxlen=LOG_LIMIT)
        self.logs.move_to_end(game_id)
        while len(self.logs) > GAME_LOG_LIMIT:
            expired_id, _ = self.logs.popitem(last=False)
            self.trades.pop(expired_id, None)
        self.sequence += 1
        state = room.deal_state
        if state and state.get("phase", "CHOICE") == "CHOICE":
            token = state.get("token")
            if token and self.trades.get(game_id, {}).get("token") != token:
                self.trades[game_id] = {"token": token, "event_id": self.sequence}
        self.logs[game_id].append({
            "id": self.sequence,
            "message": message["message"],
            "time": datetime.now(timezone.utc).isoformat(),
        })

    def game_logs(self, game_id: int, requested_page: int) -> dict:
        entries = list(reversed(self.logs.get(game_id, ())))
        total_pages = max(1, (len(entries) + LOG_PAGE_SIZE - 1) // LOG_PAGE_SIZE)
        page = min(requested_page, total_pages)
        start = (page - 1) * LOG_PAGE_SIZE
        return {
            "items": entries[start:start + LOG_PAGE_SIZE],
            "page": page,
            "total_pages": total_pages,
            "total": len(entries),
        }

    @staticmethod
    def finished_game(game, winners: list[str]) -> dict:
        def iso(value):
            if value is None:
                return None
            return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.isoformat()
        return {
            "id": game.id, "room_id": game.room_id, "status": "ENDED",
            "started_at": iso(game.started_at), "ended_at": iso(game.ended_at),
            "winners": winners, "players": [], "turn": None, "deal": None,
            "paused": False, "trade_event_id": 0,
        }

    async def load_finished_game(self, game_id: int) -> dict | None:
        # A second committed read covers a game ending/restarting while the
        # listing query was in flight. Never replace an ended card with a gap.
        async with get_session_factory()() as session:
            game = await session.scalar(select(Game).where(Game.id == game_id, Game.status == "ENDED"))
            if game is None:
                return None
            winners = list(await session.scalars(
                select(GameResult.nickname).where(GameResult.game_id == game_id, GameResult.won.is_(True))
                .order_by(GameResult.client_id)
            ))
            return self.finished_game(game, winners)

    async def snapshot(self, page=1, page_size=4, log_pages=None, request_id=0, watched_game_ids=None) -> dict:
        watched_game_ids = watched_game_ids or []
        active = and_(Room.current_game_id == Game.id, Room.status == "PLAYING", Game.status == "PLAYING")
        retained = and_(Game.status == "ENDED", Game.id.in_(watched_game_ids))
        visible = or_(active, retained)
        async with get_session_factory()() as session:
            total = await session.scalar(
                select(func.count()).select_from(Room).join(Game, Game.room_id == Room.room_id).where(visible)
            )
            finished_total = await session.scalar(select(func.count()).select_from(Game).where(retained))
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            records = list((await session.execute(
                select(Room, Game).join(Game, Game.room_id == Room.room_id)
                .where(visible).order_by(Game.started_at.desc(), Game.id.desc())
                .offset((page - 1) * page_size).limit(page_size)
            )).all())
            room_ids = [room.room_id for room, game in records if game.status == "PLAYING"]
            participants = list(await session.scalars(
                select(RoomPlayer).where(
                    RoomPlayer.room_id.in_(room_ids), RoomPlayer.left_at.is_(None)
                ).order_by(RoomPlayer.joined_at, RoomPlayer.client_id)
            )) if room_ids else []
            ended_ids = [game.id for _, game in records if game.status == "ENDED"]
            winner_records = list(await session.scalars(
                select(GameResult).where(GameResult.game_id.in_(ended_ids), GameResult.won.is_(True))
                .order_by(GameResult.client_id)
            )) if ended_ids else []

        games = []
        for db_room, game in records:
            if game.status == "ENDED":
                item = self.finished_game(game, [winner.nickname for winner in winner_records if winner.game_id == game.id])
            else:
                saved_players = [p for p in participants if p.room_id == db_room.room_id]
                # Existing game lock protects against observing uncommitted moves.
                lock = self.manager.room_locks.get(db_room.room_id)
                if lock is not None:
                    async with lock:
                        item = self.project_game(db_room, game, saved_players)
                else:
                    item = self.project_game(db_room, game, saved_players)
                if item is None:
                    item = await self.load_finished_game(game.id)
            if item is not None:
                item["logs"] = self.game_logs(game.id, (log_pages or {}).get(str(game.id), 1))
                games.append(item)
        return {
            "type": "game_status", "games": games, "total": total,
            "active_total": total - finished_total, "finished_total": finished_total,
            "page": page, "page_size": page_size, "total_pages": total_pages, "request_id": request_id,
        }

    def project_game(self, db_room, game, saved_players) -> dict | None:
        runtime = self.manager.rooms.get(db_room.room_id)
        if runtime and (runtime.status != "PLAYING" or runtime.current_game_id != game.id):
            return None  # A game ended/restarted while the DB read was in flight.
        players = []
        # Client IDs are reconnection credentials in the existing game protocol.
        # Expose only per-game display IDs, never those participant credentials.
        public_ids = {p.client_id: f"player-{index + 1}" for index, p in enumerate(saved_players)}
        names = {}
        for saved in saved_players:
            current = runtime.players.get(saved.client_id) if runtime else None
            # Only live connections belong in the spectator participant list.
            # Saved connected flags may be stale after a server restart.
            if current is not None and current.ws is not None:
                players.append(public_player(current, True, public_ids[saved.client_id]))
                names[saved.client_id] = current.nickname

        # Runtime order is authoritative; get_turn_player() may mutate turn_index.
        active_players = runtime.active_players if runtime else []
        turn = active_players[runtime.turn_index % len(active_players)] if active_players else None
        state = runtime.deal_state if runtime else db_room.deal_state
        started = game.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return {
            "id": game.id,
            "status": "PLAYING",
            "room_id": db_room.room_id,
            "started_at": started.isoformat(),
            "players": players,
            "turn": {"id": public_ids.get(turn.client_id), "nickname": turn.nickname} if turn else None,
            "deal": public_deal(state, names),
            "paused": not active_players,
            "trade_event_id": self.trades.get(game.id, {}).get("event_id", 0),
        }


def create_spectator_router(status: PublicGameStatus, base_dir: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/game-status", include_in_schema=False)
    async def page():
        return FileResponse(base_dir / "game-status.html")

    @router.get("/game-status.js", include_in_schema=False)
    async def script():
        return FileResponse(base_dir / "game-status.js", media_type="text/javascript")

    @router.get("/api/game-status")
    async def snapshot(page: int = Query(1, ge=1, le=1_000_000), page_size: int = 4):
        if page_size not in PAGE_SIZES:
            raise HTTPException(422, "화면 수는 1, 2, 4 중 선택하세요.")
        try:
            return await status.snapshot(page, page_size)
        except Exception as error:
            logger.warning("Game status unavailable (%s)", type(error).__name__)
            raise HTTPException(503, "게임 현황을 불러올 수 없습니다. 잠시 후 다시 시도해주세요.") from error

    @router.websocket("/api/game-status/ws")
    async def watch(websocket: WebSocket):
        await websocket.accept()
        subscription = parse_subscription({})
        try:
            # One connection is read-only and independent of participant sockets.
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                    if len(raw) > 65536:
                        await websocket.close(code=1009)
                        return
                    subscription = parse_subscription(json.loads(raw))
                except asyncio.TimeoutError:
                    pass
                except (ValueError, TypeError):
                    await websocket.send_json({"type": "error", "message": "조회 조건이 올바르지 않습니다."})
                    await asyncio.sleep(0.25)
                    continue
                try:
                    result = await status.snapshot(**subscription)
                    subscription["page"] = result["page"]
                    # Remember delivered games without waiting for another client
                    # request; a quick finish between refreshes must stay visible.
                    subscription["watched_game_ids"] = list(dict.fromkeys(
                        subscription["watched_game_ids"] + [game["id"] for game in result["games"]]
                    ))
                except Exception as error:
                    logger.warning("Game status unavailable (%s)", type(error).__name__)
                    result = {"type": "error", "message": "현황을 불러오지 못했습니다. 자동으로 다시 시도합니다."}
                await websocket.send_json(result)
                # Bound request rate, including clients that send continuously.
                await asyncio.sleep(0.25)
        except (WebSocketDisconnect, OSError, RuntimeError):
            return

    return router
