import asyncio
import copy
import json
import random
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import uvicorn
from database import get_session_factory
from game_logic import Player, Room
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

app = FastAPI(title="Dilemma Bingo Server")

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
    if room.status != "PLAYING" or not room.active_players:
        room.timer_task = None
        return
    if room.timer_task and not room.timer_task.done():
        return
    room.timer_task = asyncio.create_task(
        timer_wrapper(10.0, handle_turn_timeout, room.room_id, room.turn_index)
    )

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
async def handle_deal_timeout(room_id: str, deal_num: int):
    room = manager.rooms.get(room_id)
    if not room or room.status != "PLAYING" or not room.deal_state or room.deal_state["number"] != deal_num: return
    if room.timer_task is asyncio.current_task():
        room.timer_task = None
    for cid in [room.deal_state["initiator"], room.deal_state["target"]]:
        if cid not in room.deal_state["choices"]:
            room.deal_state["choices"][cid] = "BETRAY" # 무응답시 배신 처리
    await _resolve_deal(room)

# 🚨 API: 방 목록 조회 기능
@app.get("/api/rooms")
async def get_rooms():
    async with get_session_factory()() as session:
        active_rooms = await get_room_summaries(session)
    return {"rooms": active_rooms}

@app.get("/")
async def get_index():
    return FileResponse("index.html")

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
                async with manager.get_room_lock(room_id):
                    room = await manager.hydrate_room(room_id)
                    nickname = payload.get("nickname", f"User_{client_id[:4]}")
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
                    turn_player = room.get_turn_player()
                    if room.status == "PLAYING" and turn_player and turn_player.client_id == client_id:
                        state = capture_room_state(room)
                        cancel_timer(room)
                        number = payload.get("number")
                        room.last_marked_num = number
                        for p in room.active_players: p.mark_number(number)
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
                turn_player = room.get_turn_player()
                if turn_player and turn_player.client_id == client_id:
                    target_id = payload.get("target_id")
                    number = payload.get("number")
                    
                    if turn_player.last_target_id == target_id:
                        await manager.send_personal(websocket, {"type": "error", "message": "직전 턴에 거래한 대상과 연속 거래 불가!"})
                        continue

                    target_player = next(
                        (
                            player
                            for player in room.active_players
                            if player.client_id == target_id and player.client_id != client_id
                        ),
                        None,
                    )
                    if not target_player:
                        await manager.send_personal(websocket, {"type": "error", "message": "거래 대상이 유효하지 않습니다."})
                        continue

                    cancel_timer(room)
                    turn_player.last_target_id = target_id
                    room.deal_state = {"initiator": client_id, "target": target_id, "number": number, "choices": {}}
                    
                    await manager.send_personal(turn_player.ws, {"type": "open_deal", "number": number, "partner_name": target_player.nickname})
                    await manager.send_personal(target_player.ws, {"type": "open_deal", "number": number, "partner_name": turn_player.nickname})
                    await manager.broadcast(room_id, {"type": "event_log", "message": f"🤫 [비밀 거래] {turn_player.nickname}님이 {target_player.nickname}님을 지목했습니다!"})
                    
                    # 🚨 거래 타이머 시작
                    room.timer_task = asyncio.create_task(timer_wrapper(10.0, handle_deal_timeout, room.room_id, number))

            elif action == "deal_choice":
                if not room.deal_state: continue
                room.deal_state["choices"][client_id] = payload.get("choice") 
                if len(room.deal_state["choices"]) == 2:
                    cancel_timer(room)
                    await _resolve_deal(room)

            elif action == "bonus_pick":
                cancel_timer(room)
                number = payload.get("number")
                player = room.players.get(client_id)
                if player:
                    player.mark_number(number)
                    room.last_marked_num = number
                    await manager.broadcast(room_id, {"type": "event_log", "message": f"🎯 [보너스] {player.nickname}님이 보너스 칸 마킹 완료!"})
                    await _check_winners_and_next(room)

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

async def _resolve_deal(room: Room):
    initiator_id = room.deal_state["initiator"]
    target_id = room.deal_state["target"]
    number = room.deal_state["number"]
    choices = room.deal_state["choices"]
    
    p_init = room.players.get(initiator_id)
    p_target = room.players.get(target_id)
    if not p_init or not p_target:
        room.deal_state = None
        await _check_winners_and_next(room)
        return

    c_init = choices.get(initiator_id, "BETRAY")
    c_target = choices.get(target_id, "BETRAY")
    room.last_marked_num = number 

    if c_init == "COOP" and c_target == "COOP":
        p_init.mark_number(number)
        p_target.mark_number(number)
        await manager.broadcast(room.room_id, {"type": "event_log", "message": f"🤝 [상호 협동] 서로를 믿고 [{number}] 마킹 성공!"})
        room.deal_state = None
        await _check_winners_and_next(room)
    elif c_init == "BETRAY" and c_target == "BETRAY":
        for p in room.active_players:
            if p.client_id not in [initiator_id, target_id]: p.mark_number(number)
        await manager.broadcast(room.room_id, {"type": "event_log", "message": f"💥 [상호 배신 파멸!] 기회는 날아가고, 나머지 전원에게 [{number}] 공짜 마킹!"})
        room.deal_state = None
        await _check_winners_and_next(room)
    else:
        betrayer = p_init if c_init == "BETRAY" else p_target
        victim = p_target if c_init == "BETRAY" else p_init
        betrayer.mark_number(number)
        await manager.broadcast(room.room_id, {"type": "event_log", "message": f"🗡️ [배신 성공] {betrayer.nickname}님이 통수를 치고 독식했습니다!"})
        room.deal_state = None
        await manager.send_personal(betrayer.ws, {"type": "require_bonus"})
        # 보너스 픽도 10초 타이머 (무응답시 패스)
        schedule_turn_timer(room)

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