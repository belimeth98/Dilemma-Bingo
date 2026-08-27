import pytest

from game_logic import Player, Room
from main import _check_winners_and_next


def make_player(client_id: str, ws: object | None = object()) -> Player:
    return Player(client_id, client_id, ws, board=list(range(1, 50)))


def test_reconnect_preserves_player_state():
    room = Room("room-1")
    player = make_player("client-1", None)
    player.marked = {1, 2, 3}
    player.lines = 2
    player.last_target_id = "client-2"
    room.players[player.client_id] = player
    room.scores[player.client_id] = 4

    player.ws = object()
    player.nickname = "새 닉네임"
    room.activate_player(player)

    assert room.players["client-1"] is player
    assert player.marked == {1, 2, 3}
    assert player.lines == 2
    assert player.last_target_id == "client-2"
    assert room.scores["client-1"] == 4


def test_offline_players_are_excluded_from_active_players_and_turns():
    room = Room("room-1")
    offline = make_player("offline", None)
    active = make_player("active")
    room.players[offline.client_id] = offline
    room.scores[offline.client_id] = 7
    room.add_player(active)

    assert [player.client_id for player in room.active_players] == ["active"]
    assert room.get_turn_player() is active
    assert len(room.active_players) == 1


@pytest.mark.asyncio
async def test_winner_score_uses_client_id():
    room = Room("room-1")
    winner = make_player("client-1")
    winner.nickname = "same-name"
    winner.lines = 3
    room.add_player(winner)

    await _check_winners_and_next(room)

    assert room.status == "ENDED"
    assert room.scores["client-1"] == 1
    assert "same-name" not in room.scores