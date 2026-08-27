from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        Index("ix_rooms_status_updated_at", "status", "updated_at"),
    )

    room_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="WAITING")
    current_game_id: Mapped[int | None] = mapped_column(
        ForeignKey("games.id", name="fk_rooms_current_game_id", use_alter=True),
        nullable=True,
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_marked_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deal_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (
        Index("ix_games_room_started_at", "room_id", "started_at"),
        Index("ix_games_room_status", "room_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(
        ForeignKey("rooms.room_id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PLAYING")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RoomPlayer(Base):
    __tablename__ = "room_players"
    __table_args__ = (
        Index("ix_room_players_room_connected", "room_id", "connected"),
        Index("ix_room_players_room_score", "room_id", "score"),
    )

    room_id: Mapped[str] = mapped_column(
        ForeignKey("rooms.room_id", ondelete="CASCADE"), primary_key=True
    )
    client_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(100), nullable=False)
    board: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    marked: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    connected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GameResult(Base):
    __tablename__ = "game_results"
    __table_args__ = (
        Index("ix_game_results_client_id", "client_id"),
    )

    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    client_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(100), nullable=False)
    final_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    won: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    score_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
