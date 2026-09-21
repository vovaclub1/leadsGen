from pathlib import Path

import aiosqlite

from app.config import config

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# CREATE TABLE IF NOT EXISTS не добавляет колонки в уже созданную базу, поэтому новые поля
# доезжают до существующих установок отдельным списком (таблица, колонка, определение).
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("keywords", "created_at", "TEXT"),
    ("ai_usage", "ok", "INTEGER NOT NULL DEFAULT 1"),
    ("ai_usage", "error", "TEXT"),
    ("ad_posts", "image_note", "TEXT"),
)


class Database:
    def __init__(self, path: Path):
        self.path = str(path)
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        for table, column, definition in MIGRATIONS:
            cur = await self.conn.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in await cur.fetchall()}
            await cur.close()
            if columns and column not in columns:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur

    async def insert(self, sql: str, params: tuple = ()) -> int:
        cur = await self.execute(sql, params)
        return cur.lastrowid

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def scalar(self, sql: str, params: tuple = ()):
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None


db = Database(config.db_path)
