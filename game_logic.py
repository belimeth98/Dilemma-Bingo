import asyncio
import random
from typing import Dict, List, Optional, Set


class Player:
    def __init__(
        self,
        client_id: str,
        nickname: str,
        ws: object | None,
        board: Optional[List[int]] = None,
        marked: Optional[Set[int]] = None,
    ):
        self.client_id = client_id
        self.nickname = nickname
        self.ws = ws
        self.board: List[int] = board if board is not None else self._generate_board()
        self.marked: Set[int] = marked if marked is not None else set()
        self.lines: int = 0
        self.last_target_id: Optional[str] = None

    def _generate_board(self) -> List[int]:
        numbers = list(range(1, 50))
        random.shuffle(numbers)
        return numbers

    def mark_number(self, number: int):
        if number in self.board:
            self.marked.add(number)
            self.lines = self._calculate_lines()

    def _calculate_lines(self) -> int:
        lines = 0
        for row in range(7):
            if all(self.board[row * 7 + column] in self.marked for column in range(7)):
                lines += 1
        for column in range(7):
            if all(self.board[row * 7 + column] in self.marked for row in range(7)):
                lines += 1
        if all(self.board[index * 7 + index] in self.marked for index in range(7)):
            lines += 1
        if all(self.board[index * 7 + (6 - index)] in self.marked for index in range(7)):
            lines += 1
        return lines

    def get_winning_numbers(self) -> List[int]:
        if self.lines < 2:
            return []
        line_indices = []
        for row in range(7):
            line_indices.append([row * 7 + column for column in range(7)])
        for column in range(7):
            line_indices.append([row * 7 + column for row in range(7)])
        line_indices.append([index * 7 + index for index in range(7)])
        line_indices.append([index * 7 + (6 - index) for index in range(7)])

        max_marks = -1
        best_missing = []
        for indices in line_indices:
            line_numbers = [self.board[index] for index in indices]
            marked_count = sum(1 for number in line_numbers if number in self.marked)
            if marked_count == 7:
                continue
            missing_numbers = [number for number in line_numbers if number not in self.marked]
            if marked_count > max_marks:
                max_marks = marked_count
                best_missing = missing_numbers.copy()
            elif marked_count == max_marks:
                best_missing.extend(missing_numbers)
        return list(set(best_missing))

    def to_dict(self, include_board: bool = False) -> dict:
        data = {
            "id": self.client_id,
            "nickname": self.nickname,
            "lines": self.lines,
            "marked": list(self.marked),
            "winning_nums": self.get_winning_numbers(),
        }
        if include_board:
            data["board"] = self.board
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
        self.scores: Dict[str, int] = {}
        self.current_game_id: Optional[int] = None
        self.last_winners: List[str] = []
        self.timer_task: Optional[asyncio.Task] = None

    @property
    def active_players(self) -> List[Player]:
        return [
            self.players[client_id]
            for client_id in self.player_order
            if client_id in self.players and self.players[client_id].ws is not None
        ]

    def add_player(self, player: Player):
        self.players[player.client_id] = player
        if player.ws is not None and player.client_id not in self.player_order:
            self.player_order.append(player.client_id)
        if player.client_id not in self.scores:
            self.scores[player.client_id] = 0

    def activate_player(self, player: Player):
        self.players[player.client_id] = player
        if player.client_id not in self.player_order:
            self.player_order.append(player.client_id)
        if player.client_id not in self.scores:
            self.scores[player.client_id] = 0

    def remove_player(self, client_id: str):
        self.players.pop(client_id, None)
        if client_id in self.player_order:
            self.player_order.remove(client_id)
        if self.turn_index >= len(self.player_order):
            self.turn_index = 0

    def get_turn_player(self) -> Optional[Player]:
        active_players = self.active_players
        if not active_players:
            return None
        if self.turn_index >= len(active_players):
            self.turn_index = 0
        return active_players[self.turn_index]

    def next_turn(self):
        active_count = len(self.active_players)
        if active_count:
            self.turn_index = (self.turn_index + 1) % active_count

    def check_winners(self) -> List[Player]:
        return [player for player in self.active_players if player.lines >= 3]
