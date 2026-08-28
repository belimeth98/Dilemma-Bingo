from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

import main
from game_logic import Player, Room
from models import Game as GameRecord
from models import GameResult as GameResultRecord
from repositories import (
    finish_game,
    get_room_summaries,
    mark_player_disconnected,
    save_runtime_room,
)


class FakeSession:
    def __init__(self, records=None):
        self.records = records or {}
        self.commits = 0
        self.added = []
        self.flushed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def get(self, model, identity):
        if model in self.records:
            return self.records[model]
        if model is GameRecord:
            return SimpleNamespace(status="PLAYING", ended_at=None)
        return None

    async def scalars(self, statement):
        return []

    def add(self, record):
        self.added.append(record)

    async def flush(self):
        self.flushed += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def execute(self, statement):
        return statement


class FakeFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


def disconnect_websocket():
    return EndpointWebSocket([WebSocketDisconnect()])


async def run_disconnect(monkeypatch, room, client_id, websocket, saved):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))

    async def save_runtime(session, current_room, connected_client_ids=None):
        saved.append(
            {
                "turn_index": current_room.turn_index,
                "connected": set(connected_client_ids or set()),
            }
        )

    monkeypatch.setattr(main, "save_runtime_room", save_runtime)
    main.manager.rooms[room.room_id] = room
    await main.websocket_endpoint(websocket, room.room_id, client_id)


class EndpointWebSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        message = next(self.messages)
        if isinstance(message, Exception):
            raise message
        return message

    async def send_json(self, message):
        self.sent.append(message)


def make_player(client_id="client-1", ws=object()):
    return Player(client_id, client_id, ws, board=list(range(1, 50)))


@pytest.mark.asyncio
async def test_room_summaries_exclude_rooms_without_connected_players():
    class SummarySession:
        async def execute(self, statement):
            return [
                ("empty-room", "WAITING", None),
                ("waiting-room", "WAITING", "one"),
                ("waiting-room", "WAITING", "two"),
                ("playing-room", "PLAYING", "three"),
            ]

    summaries = await get_room_summaries(SummarySession())

    assert summaries == [
        {"id": "waiting-room", "players": 2, "status": "WAITING"},
        {"id": "playing-room", "players": 1, "status": "PLAYING"},
    ]


@pytest.mark.asyncio
async def test_health_checks_database_connection(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))

    response = await main.get_health()

    assert response == {"status": "ok", "database": "ok"}


@pytest.mark.asyncio
async def test_health_hides_database_errors(monkeypatch):
    class FailingSession(FakeSession):
        async def execute(self, statement):
            raise RuntimeError("connection contains a secret")

    monkeypatch.setattr(
        main,
        "get_session_factory",
        lambda: FakeFactory(FailingSession()),
    )

    with pytest.raises(main.HTTPException) as error:
        await main.get_health()

    assert error.value.status_code == 503
    assert error.value.detail == "database unavailable"


@pytest.mark.asyncio
async def test_lifespan_disposes_database_pool(monkeypatch):
    disposed = []

    async def dispose():
        disposed.append(True)

    monkeypatch.setattr(main, "dispose_engine", dispose)

    async with main.lifespan(main.app):
        assert disposed == []

    assert disposed == [True]


@pytest.mark.asyncio
async def test_index_uses_project_absolute_path():
    response = await main.get_index()

    assert Path(response.path) == main.INDEX_FILE
    assert main.INDEX_FILE.is_absolute()


@pytest.mark.asyncio
async def test_public_transition_commits_once_before_followup(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = Room("room-1")
    room.status = "PLAYING"
    room.current_game_id = 1
    room.add_player(make_player("client-1"))
    room.add_player(make_player("client-2"))
    room.players["client-1"].mark_number(1)
    room.last_marked_num = 1

    await main._check_winners_and_next(room, session=session, broadcast=False)
    await session.commit()

    assert session.commits == 1
    assert room.turn_index == 1


@pytest.mark.asyncio
async def test_hydrate_join_and_save_preserves_existing_state(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = await main.manager.hydrate_room("room-1")
    player = make_player("client-1")
    player.marked = {1, 2}
    player.lines = 2
    room.add_player(player)

    async with main.get_session_factory()() as current_session:
        await save_runtime_room(current_session, room)
        await current_session.commit()

    assert room.players["client-1"] is player
    assert player.marked == {1, 2}
    assert player.lines == 2
    assert session.commits == 2
    main.manager.rooms.pop("room-1", None)


@pytest.mark.asyncio
async def test_websocket_endpoint_join_reaches_hydrate_and_save(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    websocket = EndpointWebSocket([
        '{"action":"join","nickname":"joined"}',
        WebSocketDisconnect(),
    ])

    await main.websocket_endpoint(websocket, "room-join", "client-join")

    assert websocket.accepted
    assert any(message["type"] == "room_update" for message in websocket.sent)
    assert session.commits >= 1
    main.manager.rooms.pop("room-join", None)


@pytest.mark.asyncio
async def test_websocket_endpoint_rejects_blank_nickname(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    websocket = EndpointWebSocket([
        '{"action":"join","nickname":"   "}',
        WebSocketDisconnect(),
    ])

    await main.websocket_endpoint(websocket, "room-blank-name", "client-blank-name")

    assert websocket.accepted
    assert websocket.sent == [{"type": "error", "message": "닉네임을 입력해주세요."}]
    assert session.commits == 0
    assert "client-blank-name" not in main.manager.rooms["room-blank-name"].players
    main.manager.rooms.pop("room-blank-name", None)


def test_frontend_requires_nickname_before_connecting():
    html = main.INDEX_FILE.read_text(encoding="utf-8")
    join_specific_room = html.split("function joinSpecificRoom(roomId) {", 1)[1].split(
        "const modalDeal", 1
    )[0]
    connect_to_server = html.split("function connectToServer() {", 1)[1].split(
        "myClientId = getClientId();", 1
    )[0]

    assert "if (!nicknameInput.value.trim())" in join_specific_room
    assert "참여할 닉네임을 입력해주세요." in join_specific_room
    assert "if (!nickname)" in connect_to_server
    assert "닉네임을 입력해주세요." in connect_to_server
    assert "|| '익명'" not in connect_to_server


@pytest.mark.asyncio
async def test_start_game_failure_restores_state_and_sends_only_error(monkeypatch):
    class FailingSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.failed = False

        async def commit(self):
            self.commits += 1
            if not self.failed:
                self.failed = True
                raise RuntimeError("commit failed")

    session = FailingSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = Room("room-start-fail")
    room.add_player(make_player("one"))
    room.add_player(make_player("two"))
    original_boards = {client_id: list(player.board) for client_id, player in room.players.items()}
    main.manager.rooms[room.room_id] = room
    websocket = EndpointWebSocket(['{"action":"start_game"}', WebSocketDisconnect()])

    await main.websocket_endpoint(websocket, room.room_id, "one")

    assert room.status == "WAITING"
    assert room.current_game_id is None
    assert {client_id: player.board for client_id, player in room.players.items()} == original_boards
    assert [message["type"] for message in websocket.sent].count("error") == 1
    assert not any(message["type"] in {"game_started", "event_log", "turn_update"} for message in websocket.sent)
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_disconnect_before_current_turn_preserves_turn_client_id(monkeypatch):
    room = Room("room-disconnect-before-turn")
    players = [make_player(client_id) for client_id in ("A", "B", "C")]
    for player in players:
        room.add_player(player)
    room.turn_index = 2
    websocket = disconnect_websocket()
    players[0].ws = websocket
    saved = []
    timer_calls = []
    monkeypatch.setattr(main, "schedule_turn_timer", lambda current_room: timer_calls.append(current_room.turn_index))

    await run_disconnect(monkeypatch, room, "A", websocket, saved)

    assert room.get_turn_player().client_id == "C"
    assert room.turn_index == 1
    assert saved[0]["turn_index"] == 1
    assert saved[0]["connected"] == {"B", "C"}
    assert timer_calls == [1]
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_disconnect_current_turn_moves_to_next_and_starts_one_timer(monkeypatch):
    room = Room("room-disconnect-current-turn")
    first = make_player("A")
    current = make_player("B")
    next_player = make_player("C")
    for player in (first, current, next_player):
        room.add_player(player)
    room.turn_index = 1
    websocket = disconnect_websocket()
    current.ws = websocket
    saved = []
    timer_calls = []
    monkeypatch.setattr(main, "schedule_turn_timer", lambda current_room: timer_calls.append(current_room.turn_index))

    await run_disconnect(monkeypatch, room, "B", websocket, saved)

    assert room.get_turn_player().client_id == "C"
    assert room.turn_index == 1
    assert saved[0]["turn_index"] == 1
    assert timer_calls == [1]
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_disconnect_after_current_turn_keeps_existing_timer(monkeypatch):
    room = Room("room-disconnect-after-turn")
    first = make_player("A")
    current = make_player("B")
    leaving = make_player("C")
    for player in (first, current, leaving):
        room.add_player(player)
    room.turn_index = 0
    websocket = disconnect_websocket()
    leaving.ws = websocket
    existing_timer = __import__("asyncio").create_task(__import__("asyncio").sleep(60))
    room.timer_task = existing_timer
    saved = []
    timer_calls = []
    monkeypatch.setattr(main, "schedule_turn_timer", lambda current_room: timer_calls.append(current_room.turn_index))

    await run_disconnect(monkeypatch, room, "C", websocket, saved)

    assert room.get_turn_player().client_id == "A"
    assert room.turn_index == 0
    assert not existing_timer.cancelled()
    assert timer_calls == []
    existing_timer.cancel()
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_disconnect_saves_only_real_websocket_connections(monkeypatch):
    room = Room("room-connected-state")
    connected = make_player("connected")
    offline = make_player("offline", None)
    room.add_player(connected)
    room.players[offline.client_id] = offline
    websocket = disconnect_websocket()
    connected.ws = websocket
    saved = []

    await run_disconnect(monkeypatch, room, "connected", websocket, saved)

    assert saved[0]["connected"] == set()
    assert offline.ws is None
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_current_turn_disconnect_moves_turn_and_notifies(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = Room("room-disconnect-turn")
    room.status = "PLAYING"
    room.current_game_id = 1
    first = make_player("first")
    second = make_player("second")
    room.add_player(first)
    room.add_player(second)
    main.manager.rooms[room.room_id] = room
    websocket = EndpointWebSocket([WebSocketDisconnect()])
    first.ws = websocket
    notifications = []

    async def notify(current_room):
        notifications.append(current_room.get_turn_player().client_id)

    monkeypatch.setattr(main, "_notify_turn", notify)

    await main.websocket_endpoint(websocket, room.room_id, "first")

    assert room.get_turn_player() is second
    assert notifications == ["second"]
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_explicit_leave_stops_game_and_returns_room_to_waiting(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = Room("room-explicit-leave")
    room.status = "PLAYING"
    room.current_game_id = 1
    leaving_socket = EndpointWebSocket(
        ['{"action":"leave"}', WebSocketDisconnect()]
    )
    remaining_socket = EndpointWebSocket([])
    leaving = make_player("leaving", leaving_socket)
    remaining = make_player("remaining", remaining_socket)
    room.add_player(leaving)
    room.add_player(remaining)
    main.manager.rooms[room.room_id] = room

    await main.websocket_endpoint(leaving_socket, room.room_id, leaving.client_id)

    assert room.status == "WAITING"
    assert leaving.client_id not in room.players
    assert remaining.client_id in room.players
    assert any(
        message["type"] == "room_update" and message["status"] == "WAITING"
        for message in remaining_socket.sent
    )
    assert any(message["type"] == "game_stopped" for message in remaining_socket.sent)
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_stale_disconnect_does_not_remove_new_connection(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = Room("room-stale")
    room.status = "PLAYING"
    current = make_player("same-client")
    room.add_player(current)
    main.manager.rooms[room.room_id] = room
    old_websocket = EndpointWebSocket([WebSocketDisconnect()])
    current.ws = object()

    await main.websocket_endpoint(old_websocket, room.room_id, "same-client")

    assert room.players["same-client"] is current
    assert current.ws is not old_websocket
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_unjoined_outsider_cannot_start_game_or_broadcast(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = Room("room-outsider-start")
    room.add_player(make_player("one"))
    room.add_player(make_player("two"))
    main.manager.rooms[room.room_id] = room
    websocket = EndpointWebSocket([
        '{"action":"start_game"}',
        WebSocketDisconnect(),
    ])
    broadcasts = []

    async def broadcast(room_id, message):
        broadcasts.append(message)

    monkeypatch.setattr(main.manager, "broadcast", broadcast)

    await main.websocket_endpoint(websocket, room.room_id, "outsider")

    assert room.status == "WAITING"
    assert room.current_game_id is None
    assert session.commits == 0
    assert broadcasts == []
    assert any(message["type"] == "error" for message in websocket.sent)
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_stale_socket_cannot_run_any_game_action(monkeypatch):
    room = Room("room-stale-actions")
    current = make_player("same-client")
    room.status = "PLAYING"
    room.current_game_id = 7
    room.add_player(current)
    current_socket = object()
    current.ws = current_socket
    main.manager.rooms[room.room_id] = room
    stale = EndpointWebSocket([
        '{"action":"leave"}',
        '{"action":"public_call","number":1}',
        '{"action":"propose_deal","target_id":"other","number":1}',
        '{"action":"deal_choice","choice":"COOP"}',
        '{"action":"bonus_pick","number":1}',
        WebSocketDisconnect(),
    ])
    saved = []

    async def save_runtime(*args, **kwargs):
        saved.append(True)

    monkeypatch.setattr(main, "save_runtime_room", save_runtime)

    await main.websocket_endpoint(stale, room.room_id, "same-client")

    assert room.status == "PLAYING"
    assert room.current_game_id == 7
    assert room.players["same-client"] is current
    assert current.ws is current_socket
    assert saved == []
    assert len([message for message in stale.sent if message["type"] == "error"]) == 5
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_current_socket_can_run_start_game(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = Room("room-owned-start")
    websocket = EndpointWebSocket([
        '{"action":"start_game"}',
        WebSocketDisconnect(),
    ])
    owned = make_player("owned", websocket)
    other = make_player("other")
    room.add_player(owned)
    room.add_player(other)
    main.manager.rooms[room.room_id] = room

    await main.websocket_endpoint(websocket, room.room_id, "owned")

    assert any(message["type"] == "game_started" for message in websocket.sent)
    assert any(message["type"] == "event_log" for message in websocket.sent)
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_winner_transition_is_idempotent(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = Room("room-1")
    room.status = "PLAYING"
    room.current_game_id = 1
    winner = make_player()
    winner.lines = 3
    room.add_player(winner)

    await main._check_winners_and_next(room, session=session, broadcast=False)
    await session.commit()
    await main._check_winners_and_next(room, session=session, broadcast=False)

    assert room.scores["client-1"] == 1
    assert room.status == "ENDED"


@pytest.mark.asyncio
async def test_duplicate_game_result_is_not_inserted():
    existing = SimpleNamespace()
    session = FakeSession({GameResultRecord: existing})
    room = Room("room-1")
    room.status = "ENDED"
    room.current_game_id = 1
    winner = make_player()
    winner.lines = 3
    room.add_player(winner)
    room.scores["client-1"] = 1

    await finish_game(session, room, [winner])

    assert not any(isinstance(record, GameResultRecord) for record in session.added)


@pytest.mark.asyncio
async def test_turn_timeout_winner_commits_and_broadcasts_game_over(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = Room("room-1")
    room.status = "PLAYING"
    room.current_game_id = 1
    winner = make_player()
    winner.board[:21] = [1] * 21
    room.add_player(winner)
    main.manager.rooms[room.room_id] = room
    sent = []

    async def broadcast(room_id, message):
        sent.append(message)

    monkeypatch.setattr(main.manager, "broadcast", broadcast)
    monkeypatch.setattr(main.random, "choice", lambda values: 1)

    await main.handle_turn_timeout(room.room_id, 0)

    assert session.commits == 1
    assert any(message["type"] == "game_over" for message in sent)
    main.manager.rooms.pop(room.room_id, None)


def test_offline_deal_target_is_rejected_by_active_player_lookup():
    room = Room("room-1")
    initiator = make_player("initiator")
    offline = make_player("offline", None)
    room.add_player(initiator)
    room.players[offline.client_id] = offline

    target = next(
        (player for player in room.active_players if player.client_id == "offline"),
        None,
    )

    assert target is None


def test_playing_room_rejects_duplicate_start():
    room = Room("room-1")
    room.status = "PLAYING"
    room.add_player(make_player("client-1"))
    room.add_player(make_player("client-2"))

    with pytest.raises(ValueError, match="이미 진행 중인 게임"):
        main.validate_start_game(room)


@pytest.mark.asyncio
async def test_timeout_task_is_not_cancelled_by_its_own_cleanup():
    import asyncio

    room = Room("room-1")
    current_task = asyncio.current_task()
    room.timer_task = current_task

    main.cancel_timer(room)

    assert not current_task.cancelled()


@pytest.mark.asyncio
async def test_disconnect_state_is_saved_without_stopping_game(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room = Room("room-1")
    room.status = "PLAYING"
    room.current_game_id = 1
    player = make_player()
    room.add_player(player)
    main.manager.rooms[room.room_id] = room

    await mark_player_disconnected(session, room.room_id, player.client_id)
    await save_runtime_room(session, room, connected_client_ids=set())
    await session.commit()

    assert room.status == "PLAYING"
    assert room.current_game_id == 1
    assert player.board == list(range(1, 50))
    main.manager.rooms.pop(room.room_id, None)


def make_playing_room(room_id="room-actions"):
    room = Room(room_id)
    room.status = "PLAYING"
    room.current_game_id = 1
    initiator = make_player("initiator")
    target = make_player("target")
    room.add_player(initiator)
    room.add_player(target)
    return room, initiator, target


def test_deal_proposal_validates_turn_number_and_target():
    room, initiator, target = make_playing_room()

    current, partner = main.validate_deal_proposal(
        room, initiator.client_id, target.client_id, 1
    )

    assert current is initiator
    assert partner is target

    initiator.mark_number(1)
    with pytest.raises(ValueError, match="이미 마킹된 번호"):
        main.validate_deal_proposal(room, initiator.client_id, target.client_id, 1)

    with pytest.raises(ValueError, match="현재 차례"):
        main.validate_deal_proposal(room, target.client_id, initiator.client_id, 2)


def test_deal_choice_rejects_outsider_invalid_and_duplicate_choices():
    room, initiator, target = make_playing_room()
    outsider = make_player("outsider")
    room.add_player(outsider)
    room.deal_state = {
        "phase": "CHOICE",
        "token": "deal-token",
        "initiator": initiator.client_id,
        "target": target.client_id,
        "number": 1,
        "choices": {},
    }

    with pytest.raises(ValueError, match="거래 당사자"):
        main.validate_deal_choice(room, outsider.client_id, "COOP")

    with pytest.raises(ValueError, match="유효하지 않은 거래 선택"):
        main.validate_deal_choice(room, initiator.client_id, "INVALID")

    room.deal_state["choices"][initiator.client_id] = "COOP"
    with pytest.raises(ValueError, match="이미 거래 선택"):
        main.validate_deal_choice(room, initiator.client_id, "BETRAY")


def test_bonus_pick_requires_pending_betrayer_and_unmarked_number():
    room, initiator, target = make_playing_room()
    room.deal_state = {
        "phase": "BONUS",
        "token": "bonus-token",
        "initiator": initiator.client_id,
        "target": target.client_id,
        "number": 1,
        "choices": {initiator.client_id: "BETRAY", target.client_id: "COOP"},
        "bonus_player": initiator.client_id,
    }

    assert main.validate_bonus_pick(room, initiator.client_id, 2) is initiator

    with pytest.raises(ValueError, match="보너스 권한"):
        main.validate_bonus_pick(room, target.client_id, 2)

    initiator.mark_number(2)
    with pytest.raises(ValueError, match="이미 마킹된 번호"):
        main.validate_bonus_pick(room, initiator.client_id, 2)


@pytest.mark.asyncio
async def test_deal_proposal_is_committed_before_clients_are_notified(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room, initiator, target = make_playing_room("room-proposal-persisted")
    initiator_socket = EndpointWebSocket(
        ['{"action":"propose_deal","target_id":"target","number":1}', RuntimeError("stop")]
    )
    target_socket = EndpointWebSocket([])
    initiator.ws = initiator_socket
    target.ws = target_socket
    main.manager.rooms[room.room_id] = room

    with pytest.raises(RuntimeError, match="stop"):
        await main.websocket_endpoint(
            initiator_socket, room.room_id, initiator.client_id
        )

    assert session.commits == 1
    assert room.deal_state["phase"] == "CHOICE"
    assert room.deal_state["choices"] == {}
    assert any(message["type"] == "open_deal" for message in target_socket.sent)
    main.cancel_timer(room)
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_outsider_deal_choice_is_rejected_by_endpoint(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    room, initiator, target = make_playing_room("room-outsider-choice")
    outsider_socket = EndpointWebSocket(
        ['{"action":"deal_choice","choice":"COOP"}', WebSocketDisconnect()]
    )
    outsider = make_player("outsider", outsider_socket)
    room.add_player(outsider)
    room.deal_state = {
        "phase": "CHOICE",
        "token": "deal-token",
        "initiator": initiator.client_id,
        "target": target.client_id,
        "number": 1,
        "choices": {},
    }
    main.manager.rooms[room.room_id] = room

    await main.websocket_endpoint(outsider_socket, room.room_id, outsider.client_id)

    assert room.deal_state["choices"] == {}
    assert any(
        message["type"] == "error" and "거래 당사자" in message["message"]
        for message in outsider_socket.sent
    )
    main.cancel_timer(room)
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_deal_timeout_defaults_missing_choices_and_persists(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    monkeypatch.setattr(main, "schedule_turn_timer", lambda room: None)
    room, initiator, target = make_playing_room("room-deal-timeout")
    room.deal_state = {
        "phase": "CHOICE",
        "token": "deal-token",
        "initiator": initiator.client_id,
        "target": target.client_id,
        "number": 1,
        "choices": {},
    }
    main.manager.rooms[room.room_id] = room
    sent = []

    async def broadcast(room_id, message):
        sent.append(message)

    monkeypatch.setattr(main.manager, "broadcast", broadcast)

    await main.handle_deal_timeout(room.room_id, "deal-token")

    assert session.commits == 1
    assert room.deal_state is None
    assert room.turn_index == 1
    assert any("상호 배신" in message.get("message", "") for message in sent)
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_bonus_timeout_clears_pending_state_and_advances_turn(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(main, "get_session_factory", lambda: FakeFactory(session))
    monkeypatch.setattr(main, "schedule_turn_timer", lambda room: None)
    room, initiator, target = make_playing_room("room-bonus-timeout")
    room.deal_state = {
        "phase": "BONUS",
        "token": "bonus-token",
        "initiator": initiator.client_id,
        "target": target.client_id,
        "number": 1,
        "choices": {initiator.client_id: "BETRAY", target.client_id: "COOP"},
        "bonus_player": initiator.client_id,
    }
    main.manager.rooms[room.room_id] = room
    sent = []

    async def broadcast(room_id, message):
        sent.append(message)

    monkeypatch.setattr(main.manager, "broadcast", broadcast)

    await main.handle_bonus_timeout(room.room_id, "bonus-token")

    assert session.commits == 1
    assert room.deal_state is None
    assert room.turn_index == 1
    assert any("기회를 넘겼습니다" in message.get("message", "") for message in sent)
    main.manager.rooms.pop(room.room_id, None)


@pytest.mark.asyncio
async def test_bonus_prompt_contains_latest_marked_state(monkeypatch):
    room, initiator, target = make_playing_room("room-bonus-prompt")
    initiator.mark_number(1)
    sent = []

    async def send_personal(websocket, message):
        sent.append(message)

    async def broadcast(room_id, message):
        return None

    monkeypatch.setattr(main.manager, "send_personal", send_personal)
    monkeypatch.setattr(main.manager, "broadcast", broadcast)
    monkeypatch.setattr(main, "schedule_bonus_timer", lambda current_room: None)

    await main._publish_deal_resolution(
        room,
        {"message": "betrayal", "bonus_player": initiator.client_id},
    )

    assert sent == [
        {
            "type": "require_bonus",
            "marked": [1],
            "lines": initiator.lines,
        }
    ]


@pytest.mark.asyncio
async def test_restored_bonus_prompt_contains_latest_marked_state(monkeypatch):
    room, initiator, target = make_playing_room("room-restored-bonus")
    initiator.mark_number(1)
    room.deal_state = {
        "phase": "BONUS",
        "token": "bonus-token",
        "initiator": initiator.client_id,
        "target": target.client_id,
        "number": 1,
        "choices": {initiator.client_id: "BETRAY", target.client_id: "COOP"},
        "bonus_player": initiator.client_id,
    }
    sent = []

    async def send_personal(websocket, message):
        sent.append(message)

    monkeypatch.setattr(main.manager, "send_personal", send_personal)

    await main._restore_pending_action(room, initiator)

    assert sent == [
        {
            "type": "require_bonus",
            "marked": [1],
            "lines": initiator.lines,
        }
    ]


def test_frontend_clears_pending_ui_and_waits_for_bonus_confirmation():
    html = main.INDEX_FILE.read_text(encoding="utf-8")

    assert "function clearPendingActionUI()" in html
    for message_type in ("game_started", "turn_update", "game_over", "game_stopped"):
        message_case = html.split(f"case '{message_type}':", 1)[1].split("case '", 1)[0]
        assert "clearPendingActionUI();" in message_case

    bonus_case = html.split("case 'require_bonus':", 1)[1].split("case '", 1)[0]
    assert "Array.isArray(data.marked)" in bonus_case
    assert "isBonusMode = true;" in bonus_case

    bonus_click = html.split("if (isBonusMode) {", 1)[1].split("if (isMyTurn", 1)[0]
    assert "!isBonusSubmitting" in bonus_click
    assert "isBonusMode = false" not in bonus_click
