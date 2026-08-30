import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import main
import spectator
from game_logic import Player, Room
from models import Base, Game, Room as SavedRoom, RoomPlayer
from spectator import PublicGameStatus, create_spectator_router, parse_subscription


class ReadSession:
    """Run the real SQLAlchemy read queries against isolated SQLite, no MySQL needed."""
    def __init__(self, engine):
        self.session = Session(engine)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.session.close()

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def scalars(self, statement):
        return self.session.scalars(statement)

    async def execute(self, statement):
        return self.session.execute(statement)


@pytest.fixture
def setup_status(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    manager = main.ConnectionManager()
    status = PublicGameStatus(manager)
    monkeypatch.setattr(spectator, 'get_session_factory', lambda: lambda: ReadSession(engine))

    def seed(number, room_status='PLAYING', started=None):
        room = Room(f'room-{number}')
        room.current_game_id = number
        room.status = room_status
        for index, name in enumerate(('하늘', '바다')):
            player = Player(f'private-reconnect-{number}-{index}', name, object(), board=list(range(1, 50)))
            room.add_player(player)
        manager.rooms[room.room_id] = room
        with Session(engine) as session:
            session.add(SavedRoom(room_id=room.room_id, status=room_status, current_game_id=number))
            session.add(Game(id=number, room_id=room.room_id, status='ENDED' if room_status == 'ENDED' else 'PLAYING',
                             started_at=started or datetime(2026, 8, 30, 12) + timedelta(minutes=number)))
            for player in room.players.values():
                session.add(RoomPlayer(room_id=room.room_id, client_id=player.client_id, nickname=player.nickname,
                                       board=player.board, marked=[], connected=True, lines=0))
            session.commit()
        return room

    yield status, manager, seed, engine
    engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_sorts_by_game_start_paginates_and_filters(setup_status):
    status, _, seed, _ = setup_status
    for number in range(1, 7):
        seed(number)
    seed(7, 'WAITING')
    seed(8, 'ENDED')
    first = await status.snapshot()
    second = await status.snapshot(page=2)
    assert first['total'] == 6
    assert [g['id'] for g in first['games']] == [6, 5, 4, 3]
    assert [g['id'] for g in second['games']] == [2, 1]
    assert first['total_pages'] == 2
    for size in (1, 2, 4):
        result = await status.snapshot(page_size=size)
        assert len(result['games']) == size
        assert result['page_size'] == size
    last = await status.snapshot(page=99)
    assert last['page'] == 2


@pytest.mark.asyncio
async def test_tied_start_times_have_deterministic_order(setup_status):
    status, _, seed, _ = setup_status
    same_time = datetime(2026, 8, 30, 12)
    seed(1, started=same_time)
    seed(2, started=same_time)
    assert [g['id'] for g in (await status.snapshot())['games']] == [2, 1]


@pytest.mark.asyncio
async def test_empty_and_ended_games_clamp_page(setup_status):
    status, _, seed, engine = setup_status
    empty = await status.snapshot(page=20)
    assert empty['games'] == [] and empty['page'] == 1 and empty['total'] == 0
    room = seed(1)
    room.status = 'ENDED'
    with Session(engine) as session:
        session.get(SavedRoom, room.room_id).status = 'ENDED'
        session.get(Game, 1).status = 'ENDED'
        session.commit()
    result = await status.snapshot(page=2, page_size=1)
    assert result['games'] == [] and result['page'] == 1


@pytest.mark.asyncio
async def test_private_boards_marks_credentials_and_choices_never_leave_server(setup_status):
    status, _, seed, _ = setup_status
    room = seed(1)
    first, second = room.active_players
    for number in range(1, 21):
        first.mark_number(number)
    assert first.lines == 2
    room.deal_state = {'phase': 'CHOICE', 'initiator': first.client_id, 'target': second.client_id,
                       'number': 49, 'choices': {first.client_id: 'BETRAY'}, 'token': 'private-token'}
    result = (await status.snapshot())['games'][0]
    serialized = json.dumps(result)
    assert all(secret not in serialized for secret in ('board', 'marked', 'choices', 'private-token', 'private-reconnect', 'BETRAY'))
    assert result['players'][0]['winning_nums'] == [21]
    assert result['players'][0]['nickname'] == '하늘'
    assert result['turn'] == {'id': 'player-1', 'nickname': '하늘'}
    assert result['deal'] == {'phase': 'CHOICE', 'initiator_name': '하늘', 'target_name': '바다', 'bonus_name': None}


@pytest.mark.asyncio
async def test_reading_does_not_mutate_turn_order_timers_players_or_database(setup_status):
    status, manager, seed, engine = setup_status
    room = seed(1)
    room.turn_index = 99  # The participant getter normalizes this; observers must not.
    before = main.capture_room_state(room)
    order = list(room.player_order)
    locks = dict(manager.room_locks)
    for _ in range(3):
        await status.snapshot()
    assert before == main.capture_room_state(room)
    assert room.player_order == order
    assert manager.room_locks == locks
    assert room.timer_task is None
    with Session(engine) as session:
        assert session.get(SavedRoom, room.room_id).turn_index == 0


@pytest.mark.asyncio
async def test_snapshot_waits_for_existing_transaction_lock(setup_status):
    status, manager, seed, _ = setup_status
    room = seed(1)
    lock = manager.get_room_lock(room.room_id)
    await lock.acquire()
    task = asyncio.create_task(status.snapshot())
    await asyncio.sleep(0)
    assert not task.done()
    room.turn_index = 1
    lock.release()
    assert (await task)['games'][0]['turn']['nickname'] == '바다'


@pytest.mark.asyncio
async def test_persisted_game_survives_missing_runtime_without_fake_turn_or_room(setup_status):
    status, manager, seed, _ = setup_status
    seed(1)
    manager.rooms.clear()
    result = (await status.snapshot())['games'][0]
    assert result['paused'] and result['turn'] is None
    assert not any(p['connected'] for p in result['players'])
    assert manager.rooms == {} and manager.room_locks == {}


@pytest.mark.asyncio
async def test_turn_trade_bonus_and_log_updates_are_reflected(setup_status):
    status, _, seed, _ = setup_status
    room = seed(1)
    first, second = room.active_players
    assert (await status.snapshot())['games'][0]['deal'] is None
    room.deal_state = {'phase': 'CHOICE', 'initiator': first.client_id, 'target': second.client_id, 'number': 12, 'choices': {}}
    status.record_event(room, {'type': 'event_log', 'message': '거래 시작'})
    assert (await status.snapshot())['games'][0]['deal']['phase'] == 'CHOICE'
    room.deal_state.update(phase='BONUS', bonus_player=second.client_id)
    status.record_event(room, {'type': 'event_log', 'message': '바다 배신 성공'})
    result = (await status.snapshot())['games'][0]
    assert result['deal']['bonus_name'] == '바다'
    assert result['logs']['items'][0]['message'] == '바다 배신 성공'
    room.deal_state = None
    room.turn_index = 1
    assert (await status.snapshot())['games'][0]['turn']['nickname'] == '바다'


def test_logs_are_bounded_paginated_separate_per_game_and_public_only():
    status = PublicGameStatus(main.ConnectionManager())
    room = Room('logs')
    room.current_game_id = 1
    status.record_event(room, {'type': 'game_started', 'board': [1, 2, 3]})
    status.record_event(room, {'type': 'open_deal', 'number': 17})
    assert status.game_logs(1, 1)['total'] == 0
    for number in range(205):
        status.record_event(room, {'type': 'event_log', 'message': f'공개 콜 {number}'})
    result = status.game_logs(1, 1)
    assert result['total'] == 200 and len(result['items']) == 4 and result['total_pages'] == 50
    assert result['items'][0]['message'] == '공개 콜 204'
    assert status.game_logs(1, 2)['items'][0]['message'] == '공개 콜 200'
    room.current_game_id = 2
    assert status.game_logs(2, 1)['total'] == 0
    for number in range(2, 270):
        room.current_game_id = number
        status.record_event(room, {'type': 'event_log', 'message': '시작'})
    assert len(status.logs) == spectator.GAME_LOG_LIMIT


@pytest.mark.parametrize('payload', [None, [], {'page': 0}, {'page': True}, {'page_size': 3}, {'page_size': True},
                                    {'log_pages': {'1': 0}}, {'log_pages': {str(i): 1 for i in range(5)}},
                                    {'log_pages': {'a': 1}}, {'request_id': 'bad'}])
def test_bad_subscriptions_are_rejected(payload):
    with pytest.raises(ValueError):
        parse_subscription(payload)


@pytest.mark.asyncio
async def test_spectator_websocket_is_read_only_and_unsubscribes_cleanly(setup_status):
    status, manager, seed, _ = setup_status
    room = seed(1)
    before = main.capture_room_state(room)
    router = create_spectator_router(status, Path(__file__).resolve().parents[1])
    endpoint = next(route.endpoint for route in router.routes if route.path == '/api/game-status/ws')

    class Viewer:
        def __init__(self):
            self.incoming = iter([json.dumps({'page_size': 1, 'request_id': 1, 'action': 'start_game'}),
                                  json.dumps({'page_size': 4, 'request_id': 2})])
            self.sent = []
        async def accept(self):
            pass
        async def receive_text(self):
            try:
                return next(self.incoming)
            except StopIteration:
                raise WebSocketDisconnect()
        async def send_json(self, data):
            self.sent.append(data)

    viewer = Viewer()
    await endpoint(viewer)
    assert [r['page_size'] for r in viewer.sent] == [1, 4]
    assert [r['request_id'] for r in viewer.sent] == [1, 2]
    assert main.capture_room_state(room) == before
    assert len(room.players) == 2 and len(manager.rooms) == 1


@pytest.mark.asyncio
async def test_public_endpoint_hides_database_error_details(monkeypatch):
    status = PublicGameStatus(main.ConnectionManager())
    async def fail(*args, **kwargs):
        raise RuntimeError('secret database credential')
    monkeypatch.setattr(status, 'snapshot', fail)
    router = create_spectator_router(status, Path('.'))
    endpoint = next(route.endpoint for route in router.routes if route.path == '/api/game-status')
    with pytest.raises(main.HTTPException) as error:
        await endpoint(page=1, page_size=4)
    assert error.value.status_code == 503
    assert 'secret' not in error.value.detail
    with pytest.raises(main.HTTPException) as invalid:
        await endpoint(page=1, page_size=3)
    assert invalid.value.status_code == 422


@pytest.mark.asyncio
async def test_existing_broadcast_still_sends_unchanged_message(monkeypatch):
    manager = main.ConnectionManager()
    status = PublicGameStatus(manager)
    monkeypatch.setattr(main, 'public_game_status', status)
    room = manager.get_or_create_room('regression')
    room.current_game_id = 99
    sent = []
    class Participant:
        async def send_json(self, data):
            sent.append(data)
    room.add_player(Player('private', '참가자', Participant()))
    message = {'type': 'event_log', 'message': '📢 [공개] 참가자님이 [7]을 불렀습니다.'}
    await manager.broadcast(room.room_id, message)
    assert sent == [message]
    assert status.game_logs(99, 1)['items'][0]['message'] == message['message']

@pytest.mark.asyncio
async def test_brief_trade_still_has_notification_after_resolution(setup_status):
    status, _, seed, _ = setup_status
    room = seed(1)
    before = (await status.snapshot())['games'][0]['trade_event_id']
    first, second = room.active_players
    room.deal_state = {'phase': 'CHOICE', 'token': 'short-lived-secret',
                       'initiator': first.client_id, 'target': second.client_id}
    status.record_event(room, {'type': 'event_log', 'message': '거래 시작'})
    room.deal_state = None
    status.record_event(room, {'type': 'event_log', 'message': '협동 완료'})
    after = (await status.snapshot())['games'][0]
    assert after['deal'] is None
    assert after['trade_event_id'] > before
    assert 'short-lived-secret' not in json.dumps(after)


@pytest.mark.asyncio
async def test_failed_game_write_does_not_publish_spectator_logs(monkeypatch):
    from test_stabilization import FakeFactory, FakeSession, EndpointWebSocket
    manager = main.ConnectionManager()
    status = PublicGameStatus(manager)
    monkeypatch.setattr(main, 'manager', manager)
    monkeypatch.setattr(main, 'public_game_status', status)
    class FailingSession(FakeSession):
        async def commit(self):
            raise RuntimeError('write failure')
    monkeypatch.setattr(main, 'get_session_factory', lambda: FakeFactory(FailingSession()))
    monkeypatch.setattr(main, 'schedule_turn_timer', lambda room: None)
    room = manager.get_or_create_room('rollback')
    room.current_game_id = 1
    room.status = 'PLAYING'
    websocket = EndpointWebSocket(['{"action":"public_call","number":7}', RuntimeError('done')])
    room.add_player(Player('a', '하늘', websocket, board=list(range(1, 50))))
    room.add_player(Player('b', '바다', object(), board=list(range(1, 50))))
    with pytest.raises(RuntimeError, match='done'):
        await main.websocket_endpoint(websocket, room.room_id, 'a')
    assert room.last_marked_num is None
    assert status.game_logs(1, 1)['total'] == 0
    assert websocket.sent[-1]['type'] == 'error'
