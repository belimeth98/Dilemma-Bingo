import asyncio
import copy
import json
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import text

import uvicorn
from database import dispose_engine, get_session_factory
from game_logic import Player, Room
from spectator import PublicGameStatus, create_spectator_router
from repositories import (
    create_game,
    ensure_room,
    finish_game,
    get_room_records,
    get_room_summaries,
    mark_player_disconnected,
    mark_player_left,
    save_runtime_room,
)

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        yield
    finally:
        await dispose_engine()


app = FastAPI(title="Dilemma Bingo Server", lifespan=lifespan)

class ConnectionManager:
    def __init__(self):
        self.rooms: Dict[str, Room] = {}
        self.room_locks: Dict[str, asyncio.Lock] = {}

    def get_or_create_room(self, room_id: str) -> Room:
        if room_id not in self.rooms:
            self.rooms[room_id] = Room(room_id)
        return self.rooms[room_id]

    def get_room_lock(self, room_id: str) -> asyncio.Lock:
        if room_id not in self.room_locks:
            self.room_locks[room_id] = asyncio.Lock()
        return self.room_locks[room_id]

    async def hydrate_room(self, room_id: str) -> Room:
        room = self.get_or_create_room(room_id)
        async with get_session_factory()() as session:
            db_room, db_players = await get_room_records(session, room_id)
            if db_room is None:
                await ensure_room(session, room_id)
                await session.commit()
                return room

            room.status = db_room.status
            room.turn_index = db_room.turn_index
            room.last_marked_num = db_room.last_marked_num
            room.deal_state = db_room.deal_state
            room.current_game_id = db_room.current_game_id
            room.player_order = [
                player.client_id
                for player in room.players.values()
                if player.ws is not None
            ]
            for db_player in db_players:
                if db_player.left_at is not None:
                    continue
                player = room.players.get(db_player.client_id)
                if player is None:
                    player = Player(
                        db_player.client_id,
                        db_player.nickname,
                        None,
                        board=list(db_player.board),
                        marked=set(db_player.marked),
                    )
                    room.players[db_player.client_id] = player
                    player.lines = db_player.lines
                    player.last_target_id = db_player.last_target_id
                    room.scores[db_player.client_id] = db_player.score
                elif player.ws is None:
                    player.nickname = db_player.nickname
                room.scores[db_player.client_id] = db_player.score
        return room

    async def broadcast(self, room_id: str, message: dict):
        public_game_status.record_event(self.rooms.get(room_id), message)
        if room_id in self.rooms:
            for player in self.rooms[room_id].players.values():
                if player.ws is None:
                    continue
                try: await player.ws.send_json(message)
                except Exception: pass 

    async def send_personal(self, ws: WebSocket, message: dict):
        try: await ws.send_json(message)
        except Exception: pass

manager = ConnectionManager()
public_game_status = PublicGameStatus(manager)
app.include_router(create_spectator_router(public_game_status, BASE_DIR))


def get_current_connection_player(
    room: Room, client_id: str, websocket: WebSocket
) -> Player | None:
    player = room.players.get(client_id)
    if player is None or player.ws is None or player.ws is not websocket:
        return None
    return player


def validate_start_game(room: Room) -> None:
    if room.status == "PLAYING":
        raise ValueError("이미 진행 중인 게임입니다.")
    if len(room.active_players) < 2:
        raise ValueError("최소 2명 이상 필요합니다.")


VALID_DEAL_CHOICES = {"COOP", "BETRAY"}


def get_pending_phase(room: Room) -> str | None:
    if not room.deal_state:
        return None
    return room.deal_state.get("phase", "CHOICE")


def validate_turn_number(room: Room, client_id: str, number: object) -> Player:
    if room.status != "PLAYING":
        raise ValueError("진행 중인 게임이 아닙니다.")
    if room.deal_state is not None:
        raise ValueError("진행 중인 거래를 먼저 완료해야 합니다.")
    turn_player = room.get_turn_player()
    if turn_player is None or turn_player.client_id != client_id:
        raise ValueError("현재 차례의 참가자만 실행할 수 있습니다.")
    if type(number) is not int or number not in turn_player.board:
        raise ValueError("빙고판에 있는 유효한 번호를 선택하세요.")
    if number in turn_player.marked:
        raise ValueError("이미 마킹된 번호는 선택할 수 없습니다.")
    return turn_player


def validate_deal_proposal(
    room: Room, client_id: str, target_id: object, number: object
) -> tuple[Player, Player]:
    turn_player = validate_turn_number(room, client_id, number)
    if type(target_id) is not str or target_id == client_id:
        raise ValueError("거래 대상이 유효하지 않습니다.")
    if turn_player.last_target_id == target_id:
        raise ValueError("직전 턴에 거래한 대상과 연속 거래할 수 없습니다.")
    target_player = next(
        (player for player in room.active_players if player.client_id == target_id),
        None,
    )
    if target_player is None:
        raise ValueError("거래 대상이 유효하지 않습니다.")
    return turn_player, target_player


def validate_deal_choice(room: Room, client_id: str, choice: object) -> dict:
    state = room.deal_state
    if room.status != "PLAYING" or not state or get_pending_phase(room) != "CHOICE":
        raise ValueError("응답할 수 있는 거래가 없습니다.")
    if client_id not in {state.get("initiator"), state.get("target")}:
        raise ValueError("거래 당사자만 선택할 수 있습니다.")
    if choice not in VALID_DEAL_CHOICES:
        raise ValueError("유효하지 않은 거래 선택입니다.")
    choices = state.get("choices")
    if not isinstance(choices, dict):
        raise ValueError("거래 상태가 올바르지 않습니다.")
    if client_id in choices:
        raise ValueError("이미 거래 선택을 완료했습니다.")
    return state


def validate_bonus_pick(room: Room, client_id: str, number: object) -> Player:
    state = room.deal_state
    if room.status != "PLAYING" or not state or get_pending_phase(room) != "BONUS":
        raise ValueError("선택할 수 있는 보너스가 없습니다.")
    if state.get("bonus_player") != client_id:
        raise ValueError("보너스 권한이 있는 참가자만 선택할 수 있습니다.")
    player = room.players.get(client_id)
    if player is None or player.ws is None:
        raise ValueError("보너스 참가자를 찾을 수 없습니다.")
    if type(number) is not int or number not in player.board:
        raise ValueError("빙고판에 있는 유효한 번호를 선택하세요.")
    if number in player.marked:
        raise ValueError("이미 마킹된 번호는 선택할 수 없습니다.")
    return player


def capture_room_state(room: Room) -> dict:
    return {
        "status": room.status,
        "turn_index": room.turn_index,
        "last_marked_num": room.last_marked_num,
        "deal_state": copy.deepcopy(room.deal_state),
        "current_game_id": room.current_game_id,
        "last_winners": list(room.last_winners),
        "scores": dict(room.scores),
        "players": {
            client_id: {
                "board": list(player.board),
                "marked": set(player.marked),
                "lines": player.lines,
                "last_target_id": player.last_target_id,
                "ws": player.ws,
            }
            for client_id, player in room.players.items()
        },
    }


def restore_room_state(room: Room, state: dict) -> None:
    room.status = state["status"]
    room.turn_index = state["turn_index"]
    room.last_marked_num = state["last_marked_num"]
    room.deal_state = state["deal_state"]
    room.current_game_id = state["current_game_id"]
    room.last_winners = state["last_winners"]
    room.scores = state["scores"]
    for client_id, player_state in state["players"].items():
        player = room.players.get(client_id)
        if player is None:
            continue
        player.board = player_state["board"]
        player.marked = player_state["marked"]
        player.lines = player_state["lines"]
        player.last_target_id = player_state["last_target_id"]
        player.ws = player_state["ws"]

# 🚨 타이머 유틸리티
def cancel_timer(room: Room):
    timer_task = room.timer_task
    room.timer_task = None
    if timer_task and timer_task is not asyncio.current_task() and not timer_task.done():
        timer_task.cancel()


def schedule_turn_timer(room: Room) -> None:
    if room.status != "PLAYING" or not room.active_players or room.deal_state:
        room.timer_task = None
        return
    if room.timer_task and not room.timer_task.done():
        return
    room.timer_task = asyncio.create_task(
        timer_wrapper(10.0, handle_turn_timeout, room.room_id, room.turn_index)
    )


def schedule_deal_timer(room: Room) -> None:
    if get_pending_phase(room) != "CHOICE":
        return
    if room.timer_task and not room.timer_task.done():
        return
    token = room.deal_state.get("token") or uuid4().hex
    room.deal_state["token"] = token
    room.timer_task = asyncio.create_task(
        timer_wrapper(10.0, handle_deal_timeout, room.room_id, token)
    )


def schedule_bonus_timer(room: Room) -> None:
    if get_pending_phase(room) != "BONUS":
        return
    if room.timer_task and not room.timer_task.done():
        return
    token = room.deal_state.get("token") or uuid4().hex
    room.deal_state["token"] = token
    room.timer_task = asyncio.create_task(
        timer_wrapper(10.0, handle_bonus_timeout, room.room_id, token)
    )


def schedule_pending_timer(room: Room) -> None:
    if get_pending_phase(room) == "CHOICE":
        schedule_deal_timer(room)
    elif get_pending_phase(room) == "BONUS":
        schedule_bonus_timer(room)

async def timer_wrapper(delay: float, func, *args):
    try:
        await asyncio.sleep(delay)
        await func(*args)
    except asyncio.CancelledError:
        pass

# 타이머 핸들러: 일반 턴 초과 -> 랜덤 마킹
async def handle_turn_timeout(room_id: str, turn_index: int):
    async with manager.get_room_lock(room_id):
        room = manager.rooms.get(room_id)
        if not room or room.status != "PLAYING" or room.turn_index != turn_index or room.deal_state: return
        if room.timer_task is asyncio.current_task():
            room.timer_task = None
        player = room.get_turn_player()
        if player:
            state = capture_room_state(room)
            unmarked = [num for num in player.board if num not in player.marked]
            num = random.choice(unmarked) if unmarked else player.board[0]
            room.last_marked_num = num
            for p in room.active_players: p.mark_number(num)
            try:
                async with get_session_factory()() as session:
                    try:
                        await _check_winners_and_next(room, session=session, broadcast=False)
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        raise
            except Exception:
                restore_room_state(room, state)
                schedule_turn_timer(room)
                return
            await manager.broadcast(room_id, {"type": "event_log", "message": f"⏳ 시간 초과! {player.nickname}님이 강제로 [{num}]을(를) 불렀습니다."})
            await _broadcast_room_transition(room)

# 타이머 핸들러: 비밀 거래 초과 -> 강제 배신
async def handle_deal_timeout(room_id: str, deal_token: str):
    async with manager.get_room_lock(room_id):
        room = manager.rooms.get(room_id)
        if (
            not room
            or room.status != "PLAYING"
            or get_pending_phase(room) != "CHOICE"
            or room.deal_state.get("token") != deal_token
        ):
            return
        if room.timer_task is asyncio.current_task():
            room.timer_task = None
        state = capture_room_state(room)
        for client_id in (room.deal_state["initiator"], room.deal_state["target"]):
            room.deal_state["choices"].setdefault(client_id, "BETRAY")
        try:
            async with get_session_factory()() as session:
                try:
                    resolution = await _resolve_deal(room, session)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
        except Exception:
            restore_room_state(room, state)
            schedule_deal_timer(room)
            return
        await _publish_deal_resolution(room, resolution)


async def handle_bonus_timeout(room_id: str, bonus_token: str):
    async with manager.get_room_lock(room_id):
        room = manager.rooms.get(room_id)
        if (
            not room
            or room.status != "PLAYING"
            or get_pending_phase(room) != "BONUS"
            or room.deal_state.get("token") != bonus_token
        ):
            return
        if room.timer_task is asyncio.current_task():
            room.timer_task = None
        state = capture_room_state(room)
        bonus_player_id = room.deal_state.get("bonus_player")
        room.deal_state = None
        try:
            async with get_session_factory()() as session:
                try:
                    await _check_winners_and_next(room, session=session, broadcast=False)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
        except Exception:
            restore_room_state(room, state)
            schedule_bonus_timer(room)
            return
        bonus_player = room.players.get(bonus_player_id)
        nickname = bonus_player.nickname if bonus_player else "보너스 참가자"
        await manager.broadcast(
            room_id,
            {
                "type": "event_log",
                "message": f"⏳ {nickname}님이 보너스 번호를 선택하지 않아 기회를 넘겼습니다.",
            },
        )
        await _broadcast_room_transition(room)

# 🚨 API: 방 목록 조회 기능
@app.get("/api/rooms")
async def get_rooms():
    async with get_session_factory()() as session:
        active_rooms = await get_room_summaries(session)
    return {"rooms": active_rooms}


@app.get("/health", include_in_schema=False)
async def get_health():
    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="database unavailable",
        ) from error
    return {"status": "ok", "database": "ok"}

@app.get("/")
async def get_index():
    return FileResponse(INDEX_FILE)

@app.websocket("/ws/{room_id}/{client_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, client_id: str):
    await websocket.accept()
    room = manager.get_or_create_room(room_id)
    
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            action = payload.get("action")

            if action == "join":
                raw_nickname = payload.get("nickname")
                nickname = raw_nickname.strip() if isinstance(raw_nickname, str) else ""
                if not nickname:
                    await manager.send_personal(
                        websocket,
                        {"type": "error", "message": "닉네임을 입력해주세요."},
                    )
                    continue

                async with manager.get_room_lock(room_id):
                    room = await manager.hydrate_room(room_id)
                    player = room.players.get(client_id)

                    if room.status == "PLAYING" and player is None:
                        await manager.send_personal(websocket, {"type": "error", "message": "이미 진행 중인 게임입니다."})
                        continue

                    if player is None:
                        player = Player(client_id, nickname, websocket)
                        room.add_player(player)
                    else:
                        player.ws = websocket
                        player.nickname = nickname
                        room.activate_player(player)

                    async with get_session_factory()() as session:
                        await save_runtime_room(
                            session,
                            room,
                            connected_client_ids={
                                player_id for player_id, current in room.players.items()
                                if current.ws is not None
                            },
                        )
                        await session.commit()
                await manager.broadcast(room_id, {
                    "type": "room_update",
                    "players": [p.to_dict() for p in room.active_players],
                    "status": room.status,
                    "scores": room.scores
                })

                if room.status == "PLAYING":
                    await manager.send_personal(websocket, {
                        "type": "game_started", "board": player.board, "marked": list(player.marked)
                    })
                    if room.deal_state:
                        schedule_pending_timer(room)
                        await _restore_pending_action(room, player)
                    else:
                        await _notify_turn(room)

            elif get_current_connection_player(room, client_id, websocket) is None:
                await manager.send_personal(
                    websocket,
                    {"type": "error", "message": "현재 연결에서 실행할 수 없는 요청입니다."},
                )

            elif action == "leave": # 🚨 방 퇴장 및 중단 기능
                if client_id in room.players:
                    async with manager.get_room_lock(room_id):
                        nickname = room.players[client_id].nickname
                        room.remove_player(client_id)
                        cancel_timer(room)
                        room.status = "WAITING"
                        async with get_session_factory()() as session:
                            await mark_player_left(session, room_id, client_id)
                            await save_runtime_room(session, room)
                            await session.commit()

                    if len(room.players) == 0:
                        manager.rooms.pop(room_id, None)
                    else:
                        await manager.broadcast(room_id, {
                            "type": "room_update",
                            "players": [p.to_dict() for p in room.active_players],
                            "status": room.status,
                            "scores": room.scores
                        })
                        await manager.broadcast(room_id, {"type": "event_log", "message": f"🚨 {nickname}님이 퇴장하여 게임이 중단되었습니다."})
                        await manager.broadcast(room_id, {"type": "game_stopped"})

            elif action == "start_game":
                async with manager.get_room_lock(room_id):
                    try:
                        validate_start_game(room)
                    except ValueError as error:
                        await manager.send_personal(websocket, {"type": "error", "message": str(error)})
                        continue

                    state = capture_room_state(room)
                    room.status = "PLAYING"
                    room.turn_index = 0
                    room.last_marked_num = None
                    room.deal_state = None
                    cancel_timer(room)

                    for p in room.active_players:
                        p.board = p._generate_board()
                        p.marked = set()
                        p.lines = 0
                        p.last_target_id = None
                    try:
                        async with get_session_factory()() as session:
                            try:
                                await create_game(session, room)
                                await save_runtime_room(session, room)
                                await session.commit()
                            except Exception:
                                await session.rollback()
                                raise
                    except Exception:
                        restore_room_state(room, state)
                        schedule_turn_timer(room)
                        await manager.send_personal(websocket, {"type": "error", "message": "게임 시작 상태 저장에 실패했습니다."})
                        continue

                for p in room.active_players:
                    await manager.send_personal(p.ws, {
                        "type": "game_started", "board": p.board, "marked": list(p.marked)
                    })
                await manager.broadcast(room_id, {"type": "event_log", "message": "새로운 게임이 시작되었습니다! 3줄을 먼저 완성하세요."})
                await _notify_turn(room)

            elif action == "public_call":
                async with manager.get_room_lock(room_id):
                    number = payload.get("number")
                    try:
                        turn_player = validate_turn_number(room, client_id, number)
                    except ValueError as error:
                        await manager.send_personal(
                            websocket, {"type": "error", "message": str(error)}
                        )
                        continue
                    state = capture_room_state(room)
                    cancel_timer(room)
                    room.last_marked_num = number
                    for player in room.active_players:
                        player.mark_number(number)
                    try:
                        async with get_session_factory()() as session:
                            try:
                                await _check_winners_and_next(room, session=session, broadcast=False)
                                await session.commit()
                            except Exception:
                                await session.rollback()
                                raise
                    except Exception:
                        restore_room_state(room, state)
                        schedule_turn_timer(room)
                        await manager.send_personal(websocket, {"type": "error", "message": "공개 콜 저장에 실패했습니다."})
                        continue
                    await manager.broadcast(room_id, {"type": "event_log", "message": f"📢 [공개] {turn_player.nickname}님이 [{number}]을(를) 불렀습니다. (전원 마킹)"})
                    await _broadcast_room_transition(room)

            elif action == "propose_deal":
                async with manager.get_room_lock(room_id):
                    target_id = payload.get("target_id")
                    number = payload.get("number")
                    try:
                        turn_player, target_player = validate_deal_proposal(
                            room, client_id, target_id, number
                        )
                    except ValueError as error:
                        await manager.send_personal(
                            websocket, {"type": "error", "message": str(error)}
                        )
                        continue
                    state = capture_room_state(room)
                    cancel_timer(room)
                    turn_player.last_target_id = target_id
                    room.deal_state = {
                        "phase": "CHOICE",
                        "token": uuid4().hex,
                        "initiator": client_id,
                        "target": target_id,
                        "number": number,
                        "choices": {},
                    }
                    try:
                        async with get_session_factory()() as session:
                            try:
                                await save_runtime_room(session, room)
                                await session.commit()
                            except Exception:
                                await session.rollback()
                                raise
                    except Exception:
                        restore_room_state(room, state)
                        schedule_turn_timer(room)
                        await manager.send_personal(
                            websocket,
                            {"type": "error", "message": "거래 상태 저장에 실패했습니다."},
                        )
                        continue
                    await manager.send_personal(turn_player.ws, {"type": "open_deal", "number": number, "partner_name": target_player.nickname})
                    await manager.send_personal(target_player.ws, {"type": "open_deal", "number": number, "partner_name": turn_player.nickname})
                    await manager.broadcast(room_id, {"type": "event_log", "message": f"🤫 [비밀 거래] {turn_player.nickname}님이 {target_player.nickname}님을 지목했습니다!"})
                    schedule_deal_timer(room)

            elif action == "deal_choice":
                async with manager.get_room_lock(room_id):
                    choice = payload.get("choice")
                    try:
                        deal_state = validate_deal_choice(room, client_id, choice)
                    except ValueError as error:
                        await manager.send_personal(
                            websocket, {"type": "error", "message": str(error)}
                        )
                        continue
                    state = capture_room_state(room)
                    deal_state["choices"] = {
                        **deal_state["choices"],
                        client_id: choice,
                    }
                    resolution = None
                    deal_complete = len(deal_state["choices"]) == 2
                    if deal_complete:
                        cancel_timer(room)
                    try:
                        async with get_session_factory()() as session:
                            try:
                                if deal_complete:
                                    resolution = await _resolve_deal(room, session)
                                else:
                                    await save_runtime_room(session, room)
                                await session.commit()
                            except Exception:
                                await session.rollback()
                                raise
                    except Exception:
                        restore_room_state(room, state)
                        schedule_deal_timer(room)
                        await manager.send_personal(
                            websocket,
                            {"type": "error", "message": "거래 선택 저장에 실패했습니다."},
                        )
                        continue
                    if resolution is not None:
                        await _publish_deal_resolution(room, resolution)

            elif action == "bonus_pick":
                async with manager.get_room_lock(room_id):
                    number = payload.get("number")
                    try:
                        player = validate_bonus_pick(room, client_id, number)
                    except ValueError as error:
                        await manager.send_personal(
                            websocket, {"type": "error", "message": str(error)}
                        )
                        continue
                    state = capture_room_state(room)
                    cancel_timer(room)
                    player.mark_number(number)
                    room.last_marked_num = number
                    room.deal_state = None
                    try:
                        async with get_session_factory()() as session:
                            try:
                                await _check_winners_and_next(
                                    room, session=session, broadcast=False
                                )
                                await session.commit()
                            except Exception:
                                await session.rollback()
                                raise
                    except Exception:
                        restore_room_state(room, state)
                        schedule_bonus_timer(room)
                        await manager.send_personal(
                            websocket,
                            {"type": "error", "message": "보너스 상태 저장에 실패했습니다."},
                        )
                        continue
                    await manager.broadcast(room_id, {"type": "event_log", "message": f"🎯 [보너스] {player.nickname}님이 보너스 칸 마킹 완료!"})
                    await _broadcast_room_transition(room)

    except WebSocketDisconnect:
        if client_id in room.players:
            async with manager.get_room_lock(room_id):
                state = capture_room_state(room)
                player = room.players[client_id]
                if player.ws is not websocket:
                    return
                nickname = player.nickname
                current_turn_player = room.get_turn_player()
                current_turn_client_id = (
                    current_turn_player.client_id if current_turn_player else None
                )
                was_turn = current_turn_client_id == client_id
                previous_turn_index = room.turn_index
                player.ws = None
                active_players = room.active_players
                if was_turn:
                    if active_players:
                        room.turn_index = previous_turn_index % len(active_players)
                    else:
                        room.turn_index = 0
                elif current_turn_client_id is not None and active_players:
                    room.turn_index = next(
                        (
                            index
                            for index, active_player in enumerate(active_players)
                            if active_player.client_id == current_turn_client_id
                        ),
                        0,
                    )
                try:
                    async with get_session_factory()() as session:
                        try:
                            await mark_player_disconnected(session, room_id, client_id)
                            await save_runtime_room(
                                session,
                                room,
                                connected_client_ids={
                                    player_id for player_id in room.players
                                    if room.players[player_id].ws is not None
                                },
                            )
                            await session.commit()
                        except Exception:
                            await session.rollback()
                            raise
                except Exception:
                    restore_room_state(room, state)
                    player.ws = websocket
                    return
                room.remove_player(client_id)
                turn_index_changed = room.turn_index != previous_turn_index
                if was_turn or turn_index_changed or not room.active_players:
                    cancel_timer(room)
                schedule_pending_timer(room)
            if len(room.players) == 0:
                manager.rooms.pop(room_id, None)
            else:
                await manager.broadcast(room_id, {
                    "type": "room_update", "players": [p.to_dict() for p in room.active_players],
                    "status": room.status, "scores": room.scores
                })
                await manager.broadcast(room_id, {"type": "event_log", "message": f"🏃 {nickname}님이 연결을 종료했습니다."})
                if room.active_players and was_turn:
                    await _notify_turn(room)
                elif room.active_players and turn_index_changed:
                    await _notify_turn(room)

async def _resolve_deal(room: Room, session):
    deal_state = room.deal_state
    initiator_id = deal_state["initiator"]
    target_id = deal_state["target"]
    number = deal_state["number"]
    choices = deal_state["choices"]

    p_init = room.players.get(initiator_id)
    p_target = room.players.get(target_id)
    if not p_init or not p_target:
        room.deal_state = None
        await _check_winners_and_next(room, session=session, broadcast=False)
        return {
            "message": "거래 참가자의 연결이 종료되어 거래를 취소했습니다.",
            "bonus_player": None,
        }

    c_init = choices.get(initiator_id, "BETRAY")
    c_target = choices.get(target_id, "BETRAY")
    room.last_marked_num = number

    if c_init == "COOP" and c_target == "COOP":
        p_init.mark_number(number)
        p_target.mark_number(number)
        room.deal_state = None
        await _check_winners_and_next(room, session=session, broadcast=False)
        return {
            "message": f"🤝 [상호 협동] 서로를 믿고 [{number}] 마킹 성공!",
            "bonus_player": None,
        }
    elif c_init == "BETRAY" and c_target == "BETRAY":
        for player in room.active_players:
            if player.client_id not in [initiator_id, target_id]:
                player.mark_number(number)
        room.deal_state = None
        await _check_winners_and_next(room, session=session, broadcast=False)
        return {
            "message": f"💥 [상호 배신 파멸!] 기회는 날아가고, 나머지 전원에게 [{number}] 공짜 마킹!",
            "bonus_player": None,
        }
    else:
        betrayer = p_init if c_init == "BETRAY" else p_target
        betrayer.mark_number(number)
        room.deal_state = {
            "phase": "BONUS",
            "token": uuid4().hex,
            "initiator": initiator_id,
            "target": target_id,
            "number": number,
            "choices": choices,
            "bonus_player": betrayer.client_id,
        }
        await save_runtime_room(session, room)
        return {
            "message": f"🗡️ [배신 성공] {betrayer.nickname}님이 통수를 치고 독식했습니다!",
            "bonus_player": betrayer.client_id,
        }


async def _publish_deal_resolution(room: Room, resolution: dict) -> None:
    await manager.broadcast(
        room.room_id,
        {"type": "event_log", "message": resolution["message"]},
    )
    bonus_player_id = resolution.get("bonus_player")
    if bonus_player_id:
        bonus_player = room.players.get(bonus_player_id)
        if bonus_player and bonus_player.ws is not None:
            await manager.send_personal(
                bonus_player.ws,
                {
                    "type": "require_bonus",
                    "marked": list(bonus_player.marked),
                    "lines": bonus_player.lines,
                },
            )
        schedule_bonus_timer(room)
    else:
        await _broadcast_room_transition(room)


async def _restore_pending_action(room: Room, player: Player) -> None:
    state = room.deal_state
    if not state:
        return
    phase = get_pending_phase(room)
    if phase == "CHOICE" and player.client_id in {
        state.get("initiator"),
        state.get("target"),
    }:
        partner_id = (
            state.get("target")
            if player.client_id == state.get("initiator")
            else state.get("initiator")
        )
        partner = room.players.get(partner_id)
        await manager.send_personal(
            player.ws,
            {
                "type": "open_deal",
                "number": state.get("number"),
                "partner_name": partner.nickname if partner else "연결 대기 중",
            },
        )
    elif phase == "BONUS" and state.get("bonus_player") == player.client_id:
        await manager.send_personal(
            player.ws,
            {
                "type": "require_bonus",
                "marked": list(player.marked),
                "lines": player.lines,
            },
        )

async def _check_winners_and_next(
    room: Room,
    session=None,
    broadcast: bool = True,
):
    if room.status != "PLAYING":
        return False
    winners = room.check_winners()
    if winners:
        if room.current_game_id is None:
            raise RuntimeError("PLAYING 방에 current_game_id가 없습니다.")
        room.status = "ENDED"
        cancel_timer(room)
        winner_names = []
        for w in winners:
            winner_names.append(w.nickname)
            room.scores[w.client_id] = room.scores.get(w.client_id, 0) + 1
        room.last_winners = winner_names
        if session is None:
            async with get_session_factory()() as owned_session:
                await finish_game(owned_session, room, winners)
                await save_runtime_room(owned_session, room)
                await owned_session.commit()
        else:
            await finish_game(session, room, winners)
            await save_runtime_room(session, room)
        if broadcast:
            await manager.broadcast(room.room_id, {
                "type": "game_over", "winners": winner_names, "scores": room.scores,
                "players": [p.to_dict() for p in room.active_players]
            })
    else:
        room.next_turn()
        if session is None:
            async with get_session_factory()() as owned_session:
                await save_runtime_room(owned_session, room)
                await owned_session.commit()
        else:
            await save_runtime_room(session, room)
        if broadcast:
            await _notify_turn(room)
    return bool(winners)


async def _broadcast_room_transition(room: Room):
    if room.status == "ENDED":
        await manager.broadcast(room.room_id, {
            "type": "game_over",
            "winners": room.last_winners,
            "scores": room.scores,
            "players": [player.to_dict() for player in room.active_players],
        })
    else:
        await _notify_turn(room)

async def _notify_turn(room: Room):
    turn_player = room.get_turn_player()
    if turn_player:
        await manager.broadcast(room.room_id, {
            "type": "turn_update", "turn_client_id": turn_player.client_id,
            "turn_nickname": turn_player.nickname, "last_marked_num": room.last_marked_num,
            "players": [p.to_dict(include_board=False) for p in room.active_players],
            "scores": room.scores
        })
        schedule_turn_timer(room)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
