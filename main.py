import asyncio
import json
import random
from typing import List, Dict, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import uvicorn

app = FastAPI(title="Dilemma Bingo Server")

class Player:
    def __init__(self, client_id: str, nickname: str, ws: WebSocket):
        self.client_id = client_id
        self.nickname = nickname
        self.ws = ws
        self.board: List[int] = self._generate_board()
        self.marked: Set[int] = set()
        self.lines: int = 0
        self.last_target_id: Optional[str] = None
    
    def _generate_board(self) -> List[int]:
        nums = list(range(1, 50))
        random.shuffle(nums)
        return nums

    def mark_number(self, number: int):
        if number in self.board:
            self.marked.add(number)
            self.lines = self._calculate_lines()

    def _calculate_lines(self) -> int:
        lines = 0
        for r in range(7):
            if all(self.board[r * 7 + c] in self.marked for c in range(7)): lines += 1
        for c in range(7):
            if all(self.board[r * 7 + c] in self.marked for r in range(7)): lines += 1
        if all(self.board[i * 7 + i] in self.marked for i in range(7)): lines += 1
        if all(self.board[i * 7 + (6 - i)] in self.marked for i in range(7)): lines += 1
        return lines

    def get_winning_numbers(self) -> List[int]:
        if self.lines < 2: return []
        lines_indices = []
        for r in range(7): lines_indices.append([r * 7 + c for c in range(7)])
        for c in range(7): lines_indices.append([r * 7 + c for r in range(7)])
        lines_indices.append([i * 7 + i for i in range(7)])
        lines_indices.append([i * 7 + (6 - i) for i in range(7)])
        max_marks = -1
        best_missing = []
        for indices in lines_indices:
            line_nums = [self.board[idx] for idx in indices]
            marked_count = sum(1 for num in line_nums if num in self.marked)
            if marked_count == 7: continue
            missing_nums = [num for num in line_nums if num not in self.marked]
            if marked_count > max_marks:
                max_marks = marked_count
                best_missing = missing_nums.copy()
            elif marked_count == max_marks:
                best_missing.extend(missing_nums)
        return list(set(best_missing))

    def to_dict(self, include_board: bool = False) -> dict:
        data = {
            "id": self.client_id, "nickname": self.nickname,
            "lines": self.lines, "marked": list(self.marked),
            "winning_nums": self.get_winning_numbers()
        }
        if include_board: data["board"] = self.board
        return data

class Room:
    def __init__(self, room_id: str):
        self.room_id = room_id
        self.players: Dict[str, Player] = {}
        self.player_order: List[str] = [] 
        self.status = "WAITING" 
        self.turn_index = 0
        self.deal_state: Optional[Dict] = None
        self.last_marked_num: Optional[int] = None
        self.scores: Dict[str, int] = {} # 🚨 우승 누적 점수
        self.timer_task: Optional[asyncio.Task] = None # 🚨 10초 타이머 태스크

    def add_player(self, player: Player):
        self.players[player.client_id] = player
        if player.client_id not in self.player_order:
            self.player_order.append(player.client_id)
        if player.nickname not in self.scores:
            self.scores[player.nickname] = 0

    def remove_player(self, client_id: str):
        if client_id in self.players:
            del self.players[client_id]
            self.player_order.remove(client_id)
            if self.turn_index >= len(self.player_order):
                self.turn_index = 0

    def get_turn_player(self) -> Optional[Player]:
        if not self.player_order: return None
        return self.players.get(self.player_order[self.turn_index])

    def next_turn(self):
        if self.player_order:
            self.turn_index = (self.turn_index + 1) % len(self.player_order)

    def check_winners(self) -> List[Player]:
        return [p for p in self.players.values() if p.lines >= 3]

class ConnectionManager:
    def __init__(self):
        self.rooms: Dict[str, Room] = {}

    def get_or_create_room(self, room_id: str) -> Room:
        if room_id not in self.rooms:
            self.rooms[room_id] = Room(room_id)
        return self.rooms[room_id]

    async def broadcast(self, room_id: str, message: dict):
        if room_id in self.rooms:
            for player in self.rooms[room_id].players.values():
                try: await player.ws.send_json(message)
                except Exception: pass 

    async def send_personal(self, ws: WebSocket, message: dict):
        try: await ws.send_json(message)
        except Exception: pass

manager = ConnectionManager()

# 🚨 타이머 유틸리티
def cancel_timer(room: Room):
    if room.timer_task and not room.timer_task.done():
        room.timer_task.cancel()

async def timer_wrapper(delay: float, func, *args):
    try:
        await asyncio.sleep(delay)
        await func(*args)
    except asyncio.CancelledError:
        pass

# 타이머 핸들러: 일반 턴 초과 -> 랜덤 마킹
async def handle_turn_timeout(room_id: str, turn_index: int):
    room = manager.rooms.get(room_id)
    if not room or room.status != "PLAYING" or room.turn_index != turn_index or room.deal_state: return
    player = room.get_turn_player()
    if player:
        unmarked = [num for num in player.board if num not in player.marked]
        num = random.choice(unmarked) if unmarked else player.board[0]
        room.last_marked_num = num
        for p in room.players.values(): p.mark_number(num)
        await manager.broadcast(room_id, {"type": "event_log", "message": f"⏳ 시간 초과! {player.nickname}님이 강제로 [{num}]을(를) 불렀습니다."})
        await _check_winners_and_next(room)

# 타이머 핸들러: 비밀 거래 초과 -> 강제 배신
async def handle_deal_timeout(room_id: str, deal_num: int):
    room = manager.rooms.get(room_id)
    if not room or room.status != "PLAYING" or not room.deal_state or room.deal_state["number"] != deal_num: return
    for cid in [room.deal_state["initiator"], room.deal_state["target"]]:
        if cid not in room.deal_state["choices"]:
            room.deal_state["choices"][cid] = "BETRAY" # 무응답시 배신 처리
    await manager.broadcast(room_id, {"type": "event_log", "message": f"⏳ 10초 초과! 응답하지 않아 자동으로 [배신] 선택되었습니다."})
    await _resolve_deal(room)

# 🚨 API: 방 목록 조회 기능
@app.get("/api/rooms")
async def get_rooms():
    active_rooms = []
    for rid, r in manager.rooms.items():
        active_rooms.append({"id": rid, "players": len(r.players), "status": r.status})
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
                nickname = payload.get("nickname", f"User_{client_id[:4]}")
                player = Player(client_id, nickname, websocket)
                
                if room.status == "PLAYING" and client_id not in room.players:
                    await manager.send_personal(websocket, {"type": "error", "message": "이미 진행 중인 게임입니다."})
                    continue
                
                room.add_player(player)
                await manager.broadcast(room_id, {
                    "type": "room_update",
                    "players": [p.to_dict() for p in room.players.values()],
                    "status": room.status,
                    "scores": room.scores
                })

                if room.status == "PLAYING":
                    await manager.send_personal(websocket, {
                        "type": "game_started", "board": player.board, "marked": list(player.marked)
                    })
                    await _notify_turn(room)

            elif action == "leave": # 🚨 방 퇴장 및 중단 기능
                if client_id in room.players:
                    nickname = room.players[client_id].nickname
                    room.remove_player(client_id)
                    cancel_timer(room)
                    
                    if len(room.players) == 0:
                        del manager.rooms[room_id] # 빈 방 삭제
                    else:
                        room.status = "WAITING" # 누군가 나가면 게임 중단 후 대기 상태로
                        await manager.broadcast(room_id, {
                            "type": "room_update",
                            "players": [p.to_dict() for p in room.players.values()],
                            "status": room.status,
                            "scores": room.scores
                        })
                        await manager.broadcast(room_id, {"type": "event_log", "message": f"🚨 {nickname}님이 퇴장하여 게임이 중단되었습니다."})
                        await manager.broadcast(room_id, {"type": "game_stopped"})

            elif action == "start_game":
                if len(room.players) < 2:
                    await manager.send_personal(websocket, {"type": "error", "message": "최소 2명 이상 필요합니다."})
                    continue
                
                room.status = "PLAYING"
                room.turn_index = 0
                room.last_marked_num = None
                room.deal_state = None
                cancel_timer(room)
                
                for p in room.players.values():
                    p.board = p._generate_board()
                    p.marked = set()
                    p.lines = 0
                    p.last_target_id = None
                    await manager.send_personal(p.ws, {
                        "type": "game_started", "board": p.board, "marked": list(p.marked)
                    })
                await manager.broadcast(room_id, {"type": "event_log", "message": "새로운 게임이 시작되었습니다! 3줄을 먼저 완성하세요."})
                await _notify_turn(room)

            elif action == "public_call":
                turn_player = room.get_turn_player()
                if turn_player and turn_player.client_id == client_id:
                    cancel_timer(room)
                    number = payload.get("number")
                    room.last_marked_num = number
                    for p in room.players.values(): p.mark_number(number)
                    await manager.broadcast(room_id, {"type": "event_log", "message": f"📢 [공개] {turn_player.nickname}님이 [{number}]을(를) 불렀습니다. (전원 마킹)"})
                    await _check_winners_and_next(room)

            elif action == "propose_deal":
                turn_player = room.get_turn_player()
                if turn_player and turn_player.client_id == client_id:
                    target_id = payload.get("target_id")
                    number = payload.get("number")
                    
                    if turn_player.last_target_id == target_id:
                        await manager.send_personal(websocket, {"type": "error", "message": "직전 턴에 거래한 대상과 연속 거래 불가!"})
                        continue

                    target_player = room.players.get(target_id)
                    if not target_player: continue

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
            nickname = room.players[client_id].nickname
            room.remove_player(client_id)
            cancel_timer(room)
            if len(room.players) == 0:
                del manager.rooms[room_id]
            else:
                room.status = "WAITING"
                await manager.broadcast(room_id, {
                    "type": "room_update", "players": [p.to_dict() for p in room.players.values()],
                    "status": room.status, "scores": room.scores
                })
                await manager.broadcast(room_id, {"type": "event_log", "message": f"🏃 {nickname}님이 퇴장했습니다. 대기 모드로 전환됩니다."})
                await manager.broadcast(room_id, {"type": "game_stopped"})

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
        for p in room.players.values():
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
        room.timer_task = asyncio.create_task(timer_wrapper(10.0, handle_turn_timeout, room.room_id, room.turn_index))

async def _check_winners_and_next(room: Room):
    winners = room.check_winners()
    if winners:
        room.status = "ENDED"
        cancel_timer(room)
        winner_names = []
        for w in winners:
            winner_names.append(w.nickname)
            room.scores[w.nickname] += 1 # 🚨 우승자 점수 1점 획득
        await manager.broadcast(room.room_id, {
            "type": "game_over", "winners": winner_names, "scores": room.scores,
            "players": [p.to_dict() for p in room.players.values()]
        })
    else:
        room.next_turn()
        await _notify_turn(room)

async def _notify_turn(room: Room):
    turn_player = room.get_turn_player()
    if turn_player:
        await manager.broadcast(room.room_id, {
            "type": "turn_update", "turn_client_id": turn_player.client_id,
            "turn_nickname": turn_player.nickname, "last_marked_num": room.last_marked_num,
            "players": [p.to_dict(include_board=False) for p in room.players.values()],
            "scores": room.scores
        })
        # 🚨 일반 턴 10초 타이머 시작
        room.timer_task = asyncio.create_task(timer_wrapper(10.0, handle_turn_timeout, room.room_id, room.turn_index))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)