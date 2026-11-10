"""绑定关系与首绑礼状态的本地存储（SQLite，标准库实现）。

只存 QQ 号、星芽用户 ID、用户名和发放状态，不存密码、令牌或聊天内容。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
-- qq 列实际保存“平台用户 ID”：QQ 官方机器人是 OpenID，OneBot 是 QQ 号。
CREATE TABLE IF NOT EXISTS bindings (
    qq              TEXT PRIMARY KEY,
    newapi_user_id  INTEGER NOT NULL UNIQUE,
    username        TEXT NOT NULL,
    bound_at        INTEGER NOT NULL,
    gift_state      TEXT NOT NULL DEFAULT 'none',
    gift_quota      INTEGER NOT NULL DEFAULT 0,
    gift_at         INTEGER,
    gift_reason     TEXT,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS op_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qq          TEXT,
    action      TEXT NOT NULL,
    detail      TEXT,
    created_at  INTEGER NOT NULL
);

-- 发放台账：解绑/重绑后仍然保留，防止重复领取。
-- platform_id 与 newapi_user_id 各自唯一：一个平台用户、一个星芽账号都只能领一次。
CREATE TABLE IF NOT EXISTS gift_grants (
    platform_id     TEXT PRIMARY KEY,
    newapi_user_id  INTEGER NOT NULL UNIQUE,
    quota           INTEGER NOT NULL,
    state           TEXT NOT NULL,  -- pending|granted|unknown
    reason          TEXT,
    created_at      INTEGER NOT NULL,
    granted_at      INTEGER
);

CREATE TABLE IF NOT EXISTS lotteries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    umo            TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    prize_yadian   REAL NOT NULL,
    winners        INTEGER NOT NULL,
    draw_at        INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,
    created_by     TEXT,
    status         TEXT NOT NULL DEFAULT 'open',  -- open|drawn|cancelled
    drawn_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lotteries_status_draw
    ON lotteries(status, draw_at);
CREATE INDEX IF NOT EXISTS idx_lotteries_umo_status
    ON lotteries(umo, status);

CREATE TABLE IF NOT EXISTS lottery_entries (
    lottery_id      INTEGER NOT NULL,
    platform_id     TEXT NOT NULL,
    newapi_user_id  INTEGER NOT NULL,
    username        TEXT NOT NULL,
    joined_at       INTEGER NOT NULL,
    PRIMARY KEY (lottery_id, platform_id)
);

CREATE TABLE IF NOT EXISTS lottery_winners (
    lottery_id      INTEGER NOT NULL,
    platform_id     TEXT NOT NULL,
    newapi_user_id  INTEGER NOT NULL,
    username        TEXT NOT NULL,
    quota           INTEGER NOT NULL,
    state           TEXT NOT NULL,  -- pending|granted|failed|unknown
    reason          TEXT,
    granted_at      INTEGER,
    PRIMARY KEY (lottery_id, platform_id)
);
"""

GIFT_STATES = {"none", "pending", "granted", "failed", "unknown"}


class Store:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        async with self._lock:
            if self._conn is None:
                self._conn = await asyncio.to_thread(self._open)

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.executescript(SCHEMA)
        conn.commit()
        return conn

    async def _call(self, fn):
        async with self._lock:
            if self._conn is None:
                self._conn = await asyncio.to_thread(self._open)
            return await asyncio.to_thread(fn, self._conn)

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                conn = self._conn
                self._conn = None
                await asyncio.to_thread(conn.close)

    # ---------- 查询 ----------

    async def get_binding(self, qq: str) -> dict[str, Any] | None:
        def op(conn: sqlite3.Connection):
            row = conn.execute("SELECT * FROM bindings WHERE qq = ?", (str(qq),)).fetchone()
            return dict(row) if row else None

        return await self._call(op)

    async def get_binding_by_user_id(self, user_id: int) -> dict[str, Any] | None:
        def op(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM bindings WHERE newapi_user_id = ?", (int(user_id),)
            ).fetchone()
            return dict(row) if row else None

        return await self._call(op)

    async def find_binding(self, keyword: str) -> dict[str, Any] | None:
        keyword = str(keyword).strip()
        if keyword.isdigit():
            binding = await self.get_binding_by_user_id(int(keyword))
            if binding:
                return binding
        return await self.get_binding(keyword)

    # ---------- 写入 ----------

    async def create_binding(self, qq: str, user_id: int, username: str) -> tuple[bool, str]:
        def op(conn: sqlite3.Connection):
            if conn.execute("SELECT 1 FROM bindings WHERE qq = ?", (str(qq),)).fetchone():
                return False, "qq_bound"
            if conn.execute(
                "SELECT 1 FROM bindings WHERE newapi_user_id = ?", (int(user_id),)
            ).fetchone():
                return False, "user_bound"
            now = int(time.time())
            try:
                conn.execute(
                    """
                    INSERT INTO bindings
                        (qq, newapi_user_id, username, bound_at,
                         gift_state, gift_quota, gift_reason, updated_at)
                    VALUES (?, ?, ?, ?, 'none', 0, NULL, ?)
                    """,
                    (str(qq), int(user_id), str(username), now, now),
                )
                conn.execute(
                    "INSERT INTO op_log (qq, action, detail, created_at) VALUES (?, ?, ?, ?)",
                    (str(qq), "bind", f"user_id={int(user_id)} username={username}", now),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                return False, "conflict"
            return True, ""

        return await self._call(op)

    async def add_log(self, platform_id: str, action: str, detail: str | None = None) -> None:
        def op(conn: sqlite3.Connection):
            conn.execute(
                "INSERT INTO op_log (qq, action, detail, created_at) VALUES (?, ?, ?, ?)",
                (str(platform_id), str(action), detail, int(time.time())),
            )
            conn.commit()

        await self._call(op)

    async def delete_binding(self, qq: str) -> bool:
        def op(conn: sqlite3.Connection):
            cursor = conn.execute("DELETE FROM bindings WHERE qq = ?", (str(qq),))
            conn.execute(
                "INSERT INTO op_log (qq, action, detail, created_at) VALUES (?, ?, ?, ?)",
                (str(qq), "unbind", None, int(time.time())),
            )
            conn.commit()
            return cursor.rowcount > 0

        return await self._call(op)

    async def set_gift_state(
        self,
        qq: str,
        state: str,
        *,
        quota: int = 0,
        reason: str | None = None,
    ) -> None:
        if state not in GIFT_STATES:
            raise ValueError(f"invalid gift state: {state}")

        def op(conn: sqlite3.Connection):
            now = int(time.time())
            gift_at = now if state == "granted" else None
            conn.execute(
                """
                UPDATE bindings
                   SET gift_state = ?, gift_quota = ?, gift_at = ?,
                       gift_reason = ?, updated_at = ?
                 WHERE qq = ?
                """,
                (state, int(quota), gift_at, reason, now, str(qq)),
            )
            conn.commit()

        await self._call(op)

    async def get_gift_grant(
        self, platform_id: str | None = None, user_id: int | None = None
    ) -> dict[str, Any] | None:
        def op(conn: sqlite3.Connection):
            conditions: list[str] = []
            args: list[Any] = []
            if platform_id:
                conditions.append("platform_id = ?")
                args.append(str(platform_id))
            if user_id:
                conditions.append("newapi_user_id = ?")
                args.append(int(user_id))
            if not conditions:
                return None
            row = conn.execute(
                "SELECT * FROM gift_grants WHERE " + " OR ".join(conditions) + " LIMIT 1",
                args,
            ).fetchone()
            return dict(row) if row else None

        return await self._call(op)

    async def reserve_gift(
        self, platform_id: str, user_id: int, quota: int, reason: str
    ) -> tuple[bool, str]:
        """在调用发放接口前占位，防止并发/重启导致重复发放。"""

        def op(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT state FROM gift_grants "
                "WHERE platform_id = ? OR newapi_user_id = ? LIMIT 1",
                (str(platform_id), int(user_id)),
            ).fetchone()
            if row:
                return False, str(row["state"])
            now = int(time.time())
            try:
                conn.execute(
                    """
                    INSERT INTO gift_grants
                        (platform_id, newapi_user_id, quota, state, reason, created_at)
                    VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (str(platform_id), int(user_id), int(quota), reason, now),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                return False, "conflict"
            return True, "pending"

        return await self._call(op)

    async def complete_gift(
        self, platform_id: str, user_id: int, quota: int, reason: str
    ) -> bool:
        def op(conn: sqlite3.Connection):
            now = int(time.time())
            try:
                conn.execute(
                    """
                    INSERT INTO gift_grants
                        (platform_id, newapi_user_id, quota, state, reason,
                         created_at, granted_at)
                    VALUES (?, ?, ?, 'granted', ?, ?, ?)
                    ON CONFLICT(platform_id) DO UPDATE SET
                        state = 'granted',
                        quota = excluded.quota,
                        reason = excluded.reason,
                        granted_at = excluded.granted_at
                    """,
                    (str(platform_id), int(user_id), int(quota), reason, now, now),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # 管理员强制补发时，可能已有另一平台/账号的台账；保留原记录。
                conn.rollback()
                return False

        return await self._call(op)

    async def release_gift(self, platform_id: str) -> None:
        """明确失败时删除占位，允许用户稍后重试。"""

        def op(conn: sqlite3.Connection):
            conn.execute(
                "DELETE FROM gift_grants WHERE platform_id = ? AND state = 'pending'",
                (str(platform_id),),
            )
            conn.commit()

        await self._call(op)

    async def mark_gift_unknown(
        self, platform_id: str, user_id: int, quota: int, reason: str
    ) -> None:
        def op(conn: sqlite3.Connection):
            now = int(time.time())
            try:
                conn.execute(
                    """
                    INSERT INTO gift_grants
                        (platform_id, newapi_user_id, quota, state, reason, created_at)
                    VALUES (?, ?, ?, 'unknown', ?, ?)
                    ON CONFLICT(platform_id) DO UPDATE SET
                        state = 'unknown',
                        reason = excluded.reason
                    """,
                    (str(platform_id), int(user_id), int(quota), reason, now),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()

        await self._call(op)

    async def mark_stale_pending_unknown(self, older_than_seconds: int = 600) -> int:
        """把长时间停留在 pending 的记录标记为 unknown，避免重启后自动重复发放。"""

        def op(conn: sqlite3.Connection):
            now = int(time.time())
            threshold = now - max(0, int(older_than_seconds))
            cursor = conn.execute(
                """
                UPDATE bindings
                   SET gift_state = 'unknown', gift_reason = 'pending_timeout', updated_at = ?
                 WHERE gift_state = 'pending' AND updated_at <= ?
                """,
                (now, threshold),
            )
            grants = conn.execute(
                """
                UPDATE gift_grants
                   SET state = 'unknown', reason = 'pending_timeout'
                 WHERE state = 'pending' AND created_at <= ?
                """,
                (threshold,),
            )
            conn.commit()
            return cursor.rowcount + grants.rowcount

        return int(await self._call(op))

    # ---------- 抽奖 ----------

    async def create_lottery(
        self,
        umo: str,
        title: str,
        prize_yadian: float,
        winners: int,
        draw_at: int,
        created_by: str,
    ) -> tuple[int, str]:
        def op(conn: sqlite3.Connection):
            if conn.execute(
                "SELECT 1 FROM lotteries WHERE umo = ? AND status = 'open' LIMIT 1",
                (str(umo),),
            ).fetchone():
                return 0, "open_exists"
            now = int(time.time())
            cursor = conn.execute(
                """
                INSERT INTO lotteries
                    (umo, title, prize_yadian, winners, draw_at,
                     created_at, created_by, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    str(umo),
                    str(title),
                    float(prize_yadian),
                    int(winners),
                    int(draw_at),
                    now,
                    str(created_by),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid or 0), ""

        return await self._call(op)

    async def get_lottery(self, lottery_id: int) -> dict[str, Any] | None:
        def op(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM lotteries WHERE id = ?", (int(lottery_id),)
            ).fetchone()
            return dict(row) if row else None

        return await self._call(op)

    async def get_open_lottery(self, umo: str) -> dict[str, Any] | None:
        def op(conn: sqlite3.Connection):
            row = conn.execute(
                """
                SELECT * FROM lotteries
                 WHERE umo = ? AND status = 'open'
                 ORDER BY draw_at ASC, id ASC
                 LIMIT 1
                """,
                (str(umo),),
            ).fetchone()
            return dict(row) if row else None

        return await self._call(op)

    async def list_lotteries(
        self, *, umo: str | None = None, status: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        def op(conn: sqlite3.Connection):
            conditions: list[str] = []
            args: list[Any] = []
            if umo:
                conditions.append("umo = ?")
                args.append(str(umo))
            if status:
                conditions.append("status = ?")
                args.append(str(status))
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            rows = conn.execute(
                "SELECT * FROM lotteries" + where + " ORDER BY id DESC LIMIT ?",
                (*args, max(1, int(limit))),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(op)

    async def join_lottery(
        self,
        lottery_id: int,
        platform_id: str,
        newapi_user_id: int,
        username: str,
        *,
        now: int | None = None,
    ) -> tuple[bool, str]:
        now = int(now or time.time())

        def op(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT status, draw_at FROM lotteries WHERE id = ?", (int(lottery_id),)
            ).fetchone()
            if not row:
                return False, "not_found"
            if str(row["status"]) != "open" or int(row["draw_at"]) <= now:
                return False, "closed"
            try:
                conn.execute(
                    """
                    INSERT INTO lottery_entries
                        (lottery_id, platform_id, newapi_user_id, username, joined_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(lottery_id),
                        str(platform_id),
                        int(newapi_user_id),
                        str(username),
                        now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                return False, "joined"
            return True, ""

        return await self._call(op)

    async def count_lottery_entries(self, lottery_id: int) -> int:
        def op(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM lottery_entries WHERE lottery_id = ?",
                (int(lottery_id),),
            ).fetchone()
            return int(row["n"] if row else 0)

        return await self._call(op)

    async def list_lottery_entries(self, lottery_id: int) -> list[dict[str, Any]]:
        def op(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT * FROM lottery_entries WHERE lottery_id = ? ORDER BY joined_at ASC",
                (int(lottery_id),),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(op)

    async def get_lottery_entry(
        self, lottery_id: int, platform_id: str
    ) -> dict[str, Any] | None:
        def op(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM lottery_entries WHERE lottery_id = ? AND platform_id = ?",
                (int(lottery_id), str(platform_id)),
            ).fetchone()
            return dict(row) if row else None

        return await self._call(op)

    async def list_due_lotteries(self, now: int | None = None) -> list[dict[str, Any]]:
        now = int(now or time.time())

        def op(conn: sqlite3.Connection):
            rows = conn.execute(
                """
                SELECT * FROM lotteries
                 WHERE status = 'open' AND draw_at <= ?
                 ORDER BY draw_at ASC, id ASC
                 LIMIT 20
                """,
                (now,),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(op)

    async def finalize_lottery(
        self, lottery_id: int, winners: list[dict[str, Any]]
    ) -> bool:
        """把抽奖标记为已开奖并写入中奖记录；状态更新是原子的，防止重复开奖。"""

        def op(conn: sqlite3.Connection):
            now = int(time.time())
            cursor = conn.execute(
                "UPDATE lotteries SET status = 'drawn', drawn_at = ? "
                "WHERE id = ? AND status = 'open'",
                (now, int(lottery_id)),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            try:
                for winner in winners:
                    conn.execute(
                        """
                        INSERT INTO lottery_winners
                            (lottery_id, platform_id, newapi_user_id, username,
                             quota, state)
                        VALUES (?, ?, ?, ?, ?, 'pending')
                        """,
                        (
                            int(lottery_id),
                            str(winner["platform_id"]),
                            int(winner["newapi_user_id"]),
                            str(winner["username"]),
                            int(winner["quota"]),
                        ),
                    )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            return True

        return await self._call(op)

    async def cancel_lottery(self, lottery_id: int) -> bool:
        def op(conn: sqlite3.Connection):
            cursor = conn.execute(
                "UPDATE lotteries SET status = 'cancelled' "
                "WHERE id = ? AND status = 'open'",
                (int(lottery_id),),
            )
            conn.commit()
            return cursor.rowcount == 1

        return await self._call(op)

    async def list_lottery_winners(self, lottery_id: int) -> list[dict[str, Any]]:
        def op(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT * FROM lottery_winners WHERE lottery_id = ? "
                "ORDER BY quota DESC, username ASC",
                (int(lottery_id),),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(op)

    async def update_lottery_winner(
        self,
        lottery_id: int,
        platform_id: str,
        state: str,
        *,
        reason: str | None = None,
    ) -> None:
        if state not in {"pending", "granted", "failed", "unknown"}:
            raise ValueError(f"invalid lottery winner state: {state}")

        def op(conn: sqlite3.Connection):
            granted_at = int(time.time()) if state == "granted" else None
            conn.execute(
                """
                UPDATE lottery_winners
                   SET state = ?, reason = ?,
                       granted_at = COALESCE(?, granted_at)
                 WHERE lottery_id = ? AND platform_id = ?
                """,
                (state, reason, granted_at, int(lottery_id), str(platform_id)),
            )
            conn.commit()

        await self._call(op)

    async def mark_stale_lottery_winners_unknown(
        self, older_than_seconds: int = 600
    ) -> int:
        def op(conn: sqlite3.Connection):
            threshold = int(time.time()) - max(0, int(older_than_seconds))
            cursor = conn.execute(
                """
                UPDATE lottery_winners
                   SET state = 'unknown', reason = 'pending_timeout'
                 WHERE state = 'pending'
                   AND lottery_id IN (
                       SELECT id FROM lotteries
                        WHERE drawn_at IS NOT NULL AND drawn_at <= ?
                   )
                """,
                (threshold,),
            )
            conn.commit()
            return cursor.rowcount

        return int(await self._call(op))

    async def gift_stats(self) -> dict[str, int]:
        def op(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT gift_state, COUNT(*) AS n FROM bindings GROUP BY gift_state"
            ).fetchall()
            return {str(row["gift_state"]): int(row["n"]) for row in rows}

        return await self._call(op)
