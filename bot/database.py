import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_ts INTEGER NOT NULL UNIQUE,
    slug TEXT NOT NULL,
    side TEXT NOT NULL,
    shares INTEGER NOT NULL,
    price REAL NOT NULL,
    cost REAL NOT NULL,
    open_price REAL NOT NULL,
    window_high REAL,
    window_low REAL,
    close_price REAL,
    actual_outcome TEXT,
    pnl REAL,
    balance_after REAL,
    bet_step INTEGER,
    score REAL,
    reason TEXT,
    up_token_id TEXT,
    down_token_id TEXT,
    resolved_at INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_window_ts ON trades(window_ts);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str, initial_balance: float = 1000.0):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Idempotent column adds for DBs created before window_high/low.
            existing = {r["name"] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
            for col in ("window_high", "window_low"):
                if col not in existing:
                    c.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
            row = c.execute(
                "SELECT value FROM state WHERE key='balance'"
            ).fetchone()
            if not row:
                c.execute(
                    "INSERT INTO state(key, value) VALUES (?, ?)",
                    ("balance", str(float(initial_balance))),
                )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- balance ----------------------------------------------------------

    def get_balance(self) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM state WHERE key='balance'"
            ).fetchone()
        return float(row["value"]) if row else 0.0

    def _set_balance(self, conn, balance: float) -> None:
        conn.execute(
            "INSERT INTO state(key, value) VALUES('balance', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(float(balance)),),
        )

    # --- generic state KV --------------------------------------------------

    def get_state(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM state WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- trades -----------------------------------------------------------

    def has_trade(self, window_ts: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM trades WHERE window_ts=?", (window_ts,)
            ).fetchone()
        return row is not None

    def open_trade(
        self,
        window_ts: int,
        slug: str,
        side: str,
        shares: int,
        price: float,
        cost: float,
        open_price: float,
        score: float,
        reason: str,
        bet_step: int,
        up_token_id: str | None = None,
        down_token_id: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO trades(window_ts, slug, side, shares, price, cost,
                                   open_price, score, reason, bet_step,
                                   up_token_id, down_token_id,
                                   status, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    window_ts, slug, side, shares, price, cost,
                    open_price, score, reason, bet_step,
                    up_token_id, down_token_id, _utcnow_iso(),
                ),
            )
            return int(cur.lastrowid)

    def close_trade(
        self,
        trade_id: int,
        actual_outcome: str,
        pnl: float,
        balance_after: float,
        won: bool,
        resolved_at: int,
        open_price: float | None = None,
        close_price: float | None = None,
        window_high: float | None = None,
        window_low: float | None = None,
    ) -> None:
        """Settle a trade. When the actual Binance candle is available the
        caller should pass open/high/low/close from it — open_price is
        overwritten so the row reflects the real candle open instead of the
        pre-window estimate captured at entry time."""
        status = "won" if won else "lost"
        with self._conn() as c:
            # Build the SET clause dynamically so we only overwrite
            # open_price when a real value is supplied.
            sets = [
                "close_price=?", "window_high=?", "window_low=?",
                "actual_outcome=?", "pnl=?", "balance_after=?",
                "status=?", "closed_at=?", "resolved_at=?",
            ]
            params: list = [
                close_price, window_high, window_low,
                actual_outcome, pnl, balance_after,
                status, _utcnow_iso(), resolved_at,
            ]
            if open_price is not None:
                sets.insert(0, "open_price=?")
                params.insert(0, open_price)
            params.append(trade_id)
            c.execute(
                f"UPDATE trades SET {', '.join(sets)} WHERE id=?",
                params,
            )
            self._set_balance(c, balance_after)

    def open_trades(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades WHERE status='open' ORDER BY window_ts"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- streak / stats ---------------------------------------------------

    def get_streak(self) -> tuple[str, int]:
        """Return ('win'|'loss'|'none', count) for the most-recent resolved trades."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT status FROM trades WHERE status IN ('won','lost') "
                "ORDER BY id DESC LIMIT 100"
            ).fetchall()
        if not rows:
            return ("none", 0)
        first = rows[0]["status"]
        count = 0
        for r in rows:
            if r["status"] == first:
                count += 1
            else:
                break
        return ("win" if first == "won" else "loss", count)

    def get_streak_distribution(self) -> dict[str, dict[int, int]]:
        """Walk the whole resolved-trade history and count consecutive runs.

        Returns ``{'won': {length: count}, 'lost': {length: count}}`` — e.g.
        ``{'won': {1: 26, 2: 20, 3: 12, ...}, 'lost': {1: 27, ...}}`` meaning
        26 single wins, 20 back-to-back wins, etc.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT status FROM trades WHERE status IN ('won','lost') ORDER BY id"
            ).fetchall()
        wins: dict[int, int] = {}
        losses: dict[int, int] = {}
        if not rows:
            return {"won": wins, "lost": losses}
        cur = rows[0]["status"]; n = 1
        for r in rows[1:]:
            if r["status"] == cur:
                n += 1
            else:
                target = wins if cur == "won" else losses
                target[n] = target.get(n, 0) + 1
                cur = r["status"]; n = 1
        target = wins if cur == "won" else losses
        target[n] = target.get(n, 0) + 1
        return {"won": wins, "lost": losses}

    def get_last_n_results(self, n: int) -> list[str]:
        """Return the last ``n`` resolved trade statuses, oldest first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT status FROM trades WHERE status IN ('won','lost') "
                "ORDER BY id DESC LIMIT ?",
                (n,),
            ).fetchall()
        return [r["status"] for r in reversed(rows)]

    def stats(self) -> dict:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='won'  THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(pnl), 0) AS total_pnl
                FROM trades
                WHERE status IN ('won','lost')
                """
            ).fetchone()
        d = dict(row) if row else {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
        d["wins"] = d.get("wins") or 0
        d["losses"] = d.get("losses") or 0
        d["total"] = d.get("total") or 0
        d["total_pnl"] = float(d.get("total_pnl") or 0.0)
        return d
