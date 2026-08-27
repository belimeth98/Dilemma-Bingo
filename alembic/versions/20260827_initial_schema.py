"""Create the initial persistent game schema."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MYSQL_TABLE_OPTIONS = {
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    op.create_table(
        "rooms",
        sa.Column("room_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="WAITING", nullable=False),
        sa.Column("turn_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_marked_num", sa.Integer(), nullable=True),
        sa.Column("deal_state", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("room_id"),
        **MYSQL_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_rooms_status_updated_at", "rooms", ["status", "updated_at"]
    )

    op.create_table(
        "games",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("room_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="PLAYING", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.room_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        **MYSQL_TABLE_OPTIONS,
    )
    op.create_index("ix_games_room_started_at", "games", ["room_id", "started_at"])
    op.create_index("ix_games_room_status", "games", ["room_id", "status"])
    op.add_column(
        "rooms",
        sa.Column("current_game_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_rooms_current_game_id",
        "rooms",
        "games",
        ["current_game_id"],
        ["id"],
    )

    op.create_table(
        "room_players",
        sa.Column("room_id", sa.String(length=100), nullable=False),
        sa.Column("client_id", sa.String(length=100), nullable=False),
        sa.Column("nickname", sa.String(length=100), nullable=False),
        sa.Column("board", sa.JSON(), nullable=False),
        sa.Column("marked", sa.JSON(), nullable=False),
        sa.Column("lines", sa.Integer(), server_default="0", nullable=False),
        sa.Column("score", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_target_id", sa.String(length=100), nullable=True),
        sa.Column("connected", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.room_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("room_id", "client_id"),
        **MYSQL_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_room_players_room_connected", "room_players", ["room_id", "connected"]
    )
    op.create_index("ix_room_players_room_score", "room_players", ["room_id", "score"])

    op.create_table(
        "game_results",
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(length=100), nullable=False),
        sa.Column("nickname", sa.String(length=100), nullable=False),
        sa.Column("final_lines", sa.Integer(), server_default="0", nullable=False),
        sa.Column("won", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("score_delta", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("game_id", "client_id"),
        **MYSQL_TABLE_OPTIONS,
    )
    op.create_index("ix_game_results_client_id", "game_results", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_game_results_client_id", table_name="game_results")
    op.drop_table("game_results")

    op.drop_index("ix_room_players_room_score", table_name="room_players")
    op.drop_index("ix_room_players_room_connected", table_name="room_players")
    op.drop_table("room_players")

    op.drop_constraint("fk_rooms_current_game_id", "rooms", type_="foreignkey")
    op.drop_column("rooms", "current_game_id")
    op.drop_index("ix_games_room_status", table_name="games")
    op.drop_index("ix_games_room_started_at", table_name="games")
    op.drop_table("games")

    op.drop_index("ix_rooms_status_updated_at", table_name="rooms")
    op.drop_table("rooms")
