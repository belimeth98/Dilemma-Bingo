import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import cleanup
import main
from game_logic import Player, Room
from models import Base, Game, GameResult, Room as SavedRoom, RoomPlayer
from test_spectator import ReadSession

NOW = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)


class CleanupSession(ReadSession):
    async def get(self, *args):
        return self.session.get(*args)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


@pytest.fixture
def cleanup_db(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    manager = main.ConnectionManager()
    monkeypatch.setattr(cleanup, 'get_session_factory', lambda: lambda: CleanupSession(engine))

    def add(client_id, *, room_id='room', left_at=None, connected=False, status='WAITING'):
        with Session(engine) as session:
            if session.get(SavedRoom, room_id) is None:
                session.add(SavedRoom(room_id=room_id, status=status))
            session.add(RoomPlayer(room_id=room_id, client_id=client_id, nickname=client_id,
                                   board=list(range(1, 50)), marked=[1], score=5,
                                   left_at=left_at, last_seen_at=NOW - timedelta(days=5), connected=connected))
            session.commit()

    def remaining():
        with Session(engine) as session:
            return set(session.execute(select(RoomPlayer.room_id, RoomPlayer.client_id)))

    yield manager, engine, add, remaining
    engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_24_hour_boundary_and_preserves_disconnects_active_and_recent(cleanup_db):
    manager, engine, add, remaining = cleanup_db
    add('expired', left_at=NOW - timedelta(days=2))
    add('boundary', left_at=NOW - timedelta(hours=24))
    add('recent', left_at=NOW - timedelta(hours=24) + timedelta(seconds=1))
    add('disconnected', left_at=None)
    add('connected', left_at=NOW - timedelta(days=2), connected=True)
    add('future', left_at=NOW + timedelta(hours=1))
    assert await cleanup.cleanup_departed_players(manager, now=NOW) == 2
    assert remaining() == {('room', p) for p in ('recent', 'disconnected', 'connected', 'future')}
    assert await cleanup.cleanup_departed_players(manager, now=NOW) == 0


@pytest.mark.asyncio
async def test_cleanup_preserves_rooms_games_results_and_same_client_in_other_room(cleanup_db):
    manager, engine, add, remaining = cleanup_db
    add('departed', left_at=NOW - timedelta(days=2))
    add('departed', room_id='other', connected=True)
    with Session(engine) as session:
        session.add(Game(id=1, room_id='room', status='ENDED'))
        session.add(GameResult(game_id=1, client_id='departed', nickname='departed', final_lines=3, won=True))
        session.commit()
    assert await cleanup.cleanup_departed_players(manager, now=NOW) == 1
    assert remaining() == {('other', 'departed')}
    with Session(engine) as session:
        assert session.get(SavedRoom, 'room') is not None
        assert session.get(Game, 1).status == 'ENDED'
        assert session.get(GameResult, (1, 'departed')).won


@pytest.mark.asyncio
async def test_runtime_reconnect_and_pending_trade_participants_are_protected(cleanup_db):
    manager, engine, add, remaining = cleanup_db
    for client_id in ('online', 'initiator', 'target', 'bonus', 'expired'):
        add(client_id, left_at=NOW - timedelta(days=2), status='PLAYING')
    room = manager.get_or_create_room('room')
    room.status = 'PLAYING'
    room.add_player(Player('online', 'online', object()))
    room.deal_state = {'initiator': 'initiator', 'target': 'target', 'bonus_player': 'bonus'}
    assert await cleanup.cleanup_departed_players(manager, now=NOW) == 1
    assert remaining() == {('room', p) for p in ('online', 'initiator', 'target', 'bonus')}


@pytest.mark.asyncio
async def test_persisted_pending_trade_protected_without_runtime(cleanup_db):
    manager, engine, add, remaining = cleanup_db
    add('pending', left_at=NOW - timedelta(days=2), status='PLAYING')
    with Session(engine) as session:
        session.get(SavedRoom, 'room').deal_state = {'phase': 'CHOICE', 'target': 'pending'}
        session.commit()
    assert await cleanup.cleanup_departed_players(manager, now=NOW) == 0
    assert remaining() == {('room', 'pending')}


@pytest.mark.asyncio
async def test_cleanup_removes_stale_memory_score_without_changing_active_game(cleanup_db):
    manager, _, add, remaining = cleanup_db
    add('departed', left_at=NOW - timedelta(days=2))
    room = manager.get_or_create_room('room')
    room.status = 'PLAYING'
    room.add_player(Player('a', 'a', object()))
    room.add_player(Player('b', 'b', object()))
    room.add_player(Player('departed', 'departed', None))
    room.scores['departed'] = 5
    room.turn_index = 1
    timer = object()
    room.timer_task = timer
    before_active = list(room.active_players)
    before_order = list(room.player_order)
    assert await cleanup.cleanup_departed_players(manager, now=NOW) == 1
    assert room.active_players == before_active and room.player_order == before_order
    assert room.turn_index == 1 and room.timer_task is timer and room.status == 'PLAYING'
    assert 'departed' not in room.players and 'departed' not in room.scores


@pytest.mark.asyncio
async def test_failed_commit_rolls_back_and_does_not_clear_memory(cleanup_db, monkeypatch):
    manager, engine, add, remaining = cleanup_db
    add('departed', left_at=NOW - timedelta(days=2))
    room = manager.get_or_create_room('room')
    player = Player('departed', 'departed', None)
    room.add_player(player)
    room.scores['departed'] = 5
    class FailCommit(CleanupSession):
        async def commit(self):
            raise RuntimeError('secret database URL')
    monkeypatch.setattr(cleanup, 'get_session_factory', lambda: lambda: FailCommit(engine))
    assert await cleanup.cleanup_departed_players(manager, now=NOW) == 0
    assert remaining() == {('room', 'departed')}
    assert room.players['departed'] is player and room.scores['departed'] == 5


@pytest.mark.asyncio
async def test_rejoin_while_waiting_for_room_lock_is_rechecked(cleanup_db):
    manager, engine, add, remaining = cleanup_db
    add('rejoining', left_at=NOW - timedelta(days=2))
    lock = manager.get_room_lock('room')
    await lock.acquire()
    job = asyncio.create_task(cleanup.cleanup_departed_players(manager, now=NOW))
    try:
        await asyncio.sleep(0)
        assert not job.done()
        with Session(engine) as session:
            player = session.get(RoomPlayer, ('room', 'rejoining'))
            player.connected = True
            player.left_at = None
            session.commit()
    finally:
        lock.release()
    assert await job == 0
    assert remaining() == {('room', 'rejoining')}


@pytest.mark.asyncio
async def test_cleanup_is_bounded_per_sweep(cleanup_db, monkeypatch):
    manager, _, add, remaining = cleanup_db
    monkeypatch.setattr(cleanup, 'CLEANUP_BATCH_SIZE', 2)
    for i in range(3):
        add(str(i), left_at=NOW - timedelta(days=2))
    assert await cleanup.cleanup_departed_players(manager, now=NOW) == 2
    assert len(remaining()) == 1
    assert await cleanup.cleanup_departed_players(manager, now=NOW) == 1


@pytest.mark.asyncio
async def test_scheduler_starts_retries_on_failure_and_stops_when_cancelled(monkeypatch):
    calls = []
    retried = asyncio.Event()
    async def sweep(manager):
        calls.append(manager)
        if len(calls) == 1:
            raise RuntimeError('DB temporarily unavailable')
        retried.set()
        return 0
    monkeypatch.setattr(cleanup, 'cleanup_departed_players', sweep)
    manager = object()
    task = asyncio.create_task(cleanup.run_player_cleanup(manager, interval=0.01))
    await asyncio.wait_for(retried.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2 and all(item is manager for item in calls)


@pytest.mark.asyncio
async def test_lifespan_stops_scheduler_before_disposing_database(monkeypatch):
    order = []
    started = asyncio.Event()
    async def runner(manager):
        order.append('started')
        started.set()
        try:
            await asyncio.Future()
        finally:
            order.append('stopped')
    async def dispose():
        order.append('disposed')
    monkeypatch.setattr(main, 'run_player_cleanup', runner)
    monkeypatch.setattr(main, 'dispose_engine', dispose)
    async with main.lifespan(main.app):
        await asyncio.wait_for(started.wait(), timeout=1)
    assert order == ['started', 'stopped', 'disposed']


@pytest.mark.asyncio
async def test_default_scheduler_waits_24_hours_without_changing_retention(monkeypatch):
    waits = []
    async def sweep(manager):
        return 0
    async def stop_after_wait(delay):
        waits.append(delay)
        raise asyncio.CancelledError()
    monkeypatch.setattr(cleanup, 'cleanup_departed_players', sweep)
    monkeypatch.setattr(cleanup.asyncio, 'sleep', stop_after_wait)
    with pytest.raises(asyncio.CancelledError):
        await cleanup.run_player_cleanup(object())
    assert waits == [24 * 60 * 60]
    assert cleanup.RETENTION == timedelta(hours=24)
