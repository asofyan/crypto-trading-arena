"""SQLite wallet ledger — the durable backing store behind the in-memory arena.

``AccountStore`` keeps the trading arithmetic and an in-memory view; this module
only mirrors the resulting account state and fills to disk, so a restart
rehydrates cash/positions instead of resetting every agent to ``INITIAL_CASH``.

Durability window: the connection runs ``journal_mode=WAL`` with
``synchronous=NORMAL``. That survives a *process* crash (the WAL is replayed on the
next open) but not necessarily an OS/machine crash — the last committed fill(s)
still sitting in the WAL may be lost. That trade is deliberate: fsync-per-fill
would put disk latency on the trading hot path.

Single writer: exactly one ``read_only=False`` ``Ledger`` may own a database file
at a time (advisory ``<db>.lock``). ``read_only=True`` opens a WAL reader without
taking that lock — the safe way to probe a ledger whose engine is still running.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from arena.models import AgentAccount, TradeLogEntry

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/arena.db")
SCHEMA_VERSION = 1
RECENT_FILLS_LIMIT = 50
# meta row that makes a failed fill persist across processes: the TUI runs inside
# Live(screen=True), so a logged traceback is erased the moment the operator exits.
DEGRADED_META_KEY = "ledger_degraded"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    agent_id TEXT PRIMARY KEY,
    cash REAL NOT NULL,
    trade_count INTEGER NOT NULL,
    total_fees_paid REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    agent_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    cost_basis REAL NOT NULL,
    avg_entry_ts REAL,
    PRIMARY KEY (agent_id, product_id)
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    cash_after REAL NOT NULL,
    latency REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- summary() asks for MAX(ts) per agent; without this it scans every fill.
CREATE INDEX IF NOT EXISTS fills_agent_ts ON fills(agent_id, ts);
"""

_ISO_WITH_SECONDS = "%H:%M:%S"


class LedgerSchemaError(RuntimeError):
    """The on-disk database was written by an incompatible schema version."""


class LedgerInUseError(RuntimeError):
    """Another process already holds the single-writer lock for this database."""


def _format_fill_ts(ts: str) -> str:
    """Render a stored ISO timestamp as HH:MM:SS for the recent-trades panel."""
    try:
        return datetime.fromisoformat(ts).strftime(_ISO_WITH_SECONDS)
    except ValueError:
        # Legacy/hand-edited rows: show whatever is there rather than dropping the fill.
        return ts


class Ledger:
    """SQLite persistence for arena accounts, positions, and fills.

    One ``read_only=False`` instance owns the file (advisory ``<db>.lock`` lock);
    any number of ``read_only=True`` instances may read it concurrently.
    """

    def __init__(self, path: str | Path = DEFAULT_DB_PATH, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        # Tool nodes are invoked from worker threads, so the connection is shared
        # across threads; every statement below runs under self._lock.
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._lock_fd: int | None = None

        if read_only:
            self._open_read_only()
        else:
            self._open_read_write()

    @property
    def lock_path(self) -> Path:
        """Sidecar file whose advisory lock marks the owning writer."""
        return self.path.with_name(f"{self.path.name}.lock")

    def _open_read_write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_writer_lock()
        try:
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn = conn
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._check_schema_version()
        except BaseException:
            # Never leak the lock or the connection when bootstrapping fails — a
            # retry would otherwise report LedgerInUseError instead of the real error.
            self.close()
            raise

    def _open_read_only(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"{self.path}: no ledger database at this path")
        # mode=ro keeps this connection from ever creating or migrating the file;
        # WAL readers are safe next to a live writer, so no lock is taken here.
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            self._check_schema_version()
        except BaseException:
            self.close()
            raise

    def _acquire_writer_lock(self) -> None:
        """Take the exclusive advisory lock, or refuse to open a second writer."""
        lock_path = self.lock_path
        # An OSError here (EACCES, ENOSPC, EISDIR) says the *filesystem* is unhappy, not
        # that someone else owns the ledger — let it surface unmapped.
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise LedgerInUseError(
                f"{lock_path}: another engine already owns this ledger (single-writer "
                f"lock is held). Stop that engine first, or point the engine at a "
                f"different database."
            ) from exc
        self._lock_fd = fd

    def _release_writer_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None

    # ── Lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        """Close the connection and release the writer lock. Safe to call twice."""
        with self._lock:
            conn, self._conn = self._conn, None
            try:
                if conn is not None:
                    conn.close()
            finally:
                self._release_writer_lock()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Ledger is closed")
        return self._conn

    def _check_schema_version(self) -> None:
        conn = self._db()
        # No blanket wrapper: only a version mismatch (or a missing version row) is a
        # schema problem. "file is not a database" / "unable to open database file"
        # are sqlite errors and must keep their own identity, otherwise the operator
        # is told to migrate a file that was never a ledger.
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()

        if row is None:
            if self.read_only:
                self.close()
                raise LedgerSchemaError(
                    f"{self.path}: arena ledger has no schema_version row — it was "
                    f"never initialised or was written by an older version."
                )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
            return

        try:
            found: int | None = int(row[0])
        except (TypeError, ValueError):
            found = None
        if found != SCHEMA_VERSION:
            self.close()
            raise LedgerSchemaError(
                f"{self.path}: ledger schema version mismatch (found {row[0]!r}, "
                f"expected {SCHEMA_VERSION}). Refusing to open a database written by a "
                f"different schema version — migrate it or move it aside."
            )

    # ── Degraded marker (cross-process operator visibility) ───────

    def mark_degraded(self, agent_id: str, timestamp: str) -> None:
        """Persist that a fill could not be written, so it outlives this process.

        ``AccountStore`` keeps the fill in memory and raises nothing, so without this
        row a divergence is invisible to ``manage_bot.py status`` and to anything
        reading the ledger after the engine (or its alternate-screen TUI) is gone.
        """
        payload = json.dumps({"ts": timestamp, "agent": agent_id})
        with self._lock:
            conn = self._db()
            with conn:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (DEGRADED_META_KEY, payload),
                )

    def mark_healthy(self) -> None:
        """Clear the degraded marker: this writer just hydrated from disk cleanly."""
        with self._lock:
            conn = self._db()
            with conn:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, '') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (DEGRADED_META_KEY,),
                )

    def degraded_marker(self) -> dict[str, str] | None:
        """The last recorded divergence as ``{"ts", "agent"}``, or ``None`` when healthy."""
        with self._lock:
            row = self._db().execute(
                "SELECT value FROM meta WHERE key = ?", (DEGRADED_META_KEY,)
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            # Hand-edited/legacy value: still flag it, just without the details.
            return {"ts": str(row[0]), "agent": "unknown"}
        if not isinstance(payload, dict):
            return {"ts": str(row[0]), "agent": "unknown"}
        return {
            "ts": str(payload.get("ts", "")),
            "agent": str(payload.get("agent", "unknown")),
        }

    # ── Reads ────────────────────────────────────────────────────

    def load_accounts(self) -> dict[str, AgentAccount]:
        """Hydrate every persisted account, including its open positions."""
        with self._lock:
            conn = self._db()
            accounts = {
                agent_id: AgentAccount(
                    cash=cash,
                    trade_count=trade_count,
                    total_fees_paid=total_fees_paid,
                    realized_pnl=realized_pnl,
                )
                for agent_id, cash, trade_count, total_fees_paid, realized_pnl in conn.execute(
                    "SELECT agent_id, cash, trade_count, total_fees_paid, realized_pnl "
                    "FROM accounts"
                )
            }
            position_rows = conn.execute(
                "SELECT agent_id, product_id, quantity, cost_basis, avg_entry_ts FROM positions"
            ).fetchall()

        for agent_id, product_id, quantity, cost_basis, avg_entry_ts in position_rows:
            account = accounts.get(agent_id)
            if account is None:
                log.warning(
                    "ledger.load_accounts: skipping orphan position %s/%s (no account row)",
                    agent_id, product_id,
                )
                continue
            account.positions[product_id] = quantity
            account.cost_basis[product_id] = cost_basis
            if avg_entry_ts is not None:
                account.avg_entry_ts[product_id] = float(avg_entry_ts)

        log.debug("ledger.load_accounts: loaded %d account(s) from %s", len(accounts), self.path)
        return accounts

    def recent_fills(self, limit: int = RECENT_FILLS_LIMIT) -> list[TradeLogEntry]:
        """The newest ``limit`` fills, returned oldest→newest for a chronological panel."""
        with self._lock:
            rows = self._db().execute(
                "SELECT ts, agent_id, action, product_id, quantity, price, fee, latency "
                "FROM fills ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [
            TradeLogEntry(
                _format_fill_ts(ts), agent_id, action, product_id, quantity, price, fee, latency
            )
            for ts, agent_id, action, product_id, quantity, price, fee, latency in reversed(rows)
        ]

    def summary(self) -> list[dict]:
        """One row per agent for the CLI ``status`` command."""
        with self._lock:
            rows = self._db().execute(
                "SELECT a.agent_id, a.cash, a.trade_count, a.realized_pnl, a.total_fees_paid, "
                "(SELECT COUNT(*) FROM positions p WHERE p.agent_id = a.agent_id), "
                "(SELECT MAX(f.ts) FROM fills f WHERE f.agent_id = a.agent_id) "
                "FROM accounts a ORDER BY a.agent_id"
            ).fetchall()

        return [
            {
                "agent_id": agent_id,
                "cash": cash,
                "position_count": position_count,
                "trade_count": trade_count,
                "realized_pnl": realized_pnl,
                "total_fees_paid": total_fees_paid,
                "last_fill_ts": last_fill_ts,
            }
            for (
                agent_id,
                cash,
                trade_count,
                realized_pnl,
                total_fees_paid,
                position_count,
                last_fill_ts,
            ) in rows
        ]

    # ── Writes ───────────────────────────────────────────────────

    def save_fill(
        self,
        *,
        agent_id: str,
        account: AgentAccount,
        timestamp: str,
        action: str,
        product_id: str,
        quantity: float,
        price: float,
        fee: float,
        cash_after: float,
        latency: float | None,
    ) -> None:
        """Persist one executed fill plus the account state it produced, atomically."""
        with self._lock:
            conn = self._db()
            # Snapshot before writing: another thread may mutate these mappings
            # mid-loop (a concurrent buy/sell on the same agent), which would
            # otherwise raise "dictionary changed size during iteration" or persist
            # a torn state.
            positions = dict(account.positions)
            cost_basis = dict(account.cost_basis)
            avg_entry_ts = dict(account.avg_entry_ts)
            with conn:  # one transaction: account + positions + fill land together
                conn.execute(
                    "INSERT INTO accounts "
                    "(agent_id, cash, trade_count, total_fees_paid, realized_pnl, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(agent_id) DO UPDATE SET "
                    "cash = excluded.cash, "
                    "trade_count = excluded.trade_count, "
                    "total_fees_paid = excluded.total_fees_paid, "
                    "realized_pnl = excluded.realized_pnl, "
                    "updated_at = excluded.updated_at",
                    (
                        agent_id,
                        account.cash,
                        account.trade_count,
                        account.total_fees_paid,
                        account.realized_pnl,
                        timestamp,
                    ),
                )
                for pid, qty in positions.items():
                    conn.execute(
                        "INSERT INTO positions "
                        "(agent_id, product_id, quantity, cost_basis, avg_entry_ts) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(agent_id, product_id) DO UPDATE SET "
                        "quantity = excluded.quantity, "
                        "cost_basis = excluded.cost_basis, "
                        "avg_entry_ts = excluded.avg_entry_ts",
                        (
                            agent_id,
                            pid,
                            qty,
                            cost_basis.get(pid, 0.0),
                            avg_entry_ts.get(pid),
                        ),
                    )
                # A full sell drops the key from account.positions; mirror that on disk.
                self._delete_stale_positions(conn, agent_id, positions)
                conn.execute(
                    "INSERT INTO fills "
                    "(ts, agent_id, action, product_id, quantity, price, fee, cash_after, latency) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (timestamp, agent_id, action, product_id, quantity, price, fee, cash_after,
                     latency),
                )

        log.debug(
            "ledger.save_fill: agent=%s %s %s qty=%s cash_after=%.2f",
            agent_id, action, product_id, quantity, cash_after,
        )

    @staticmethod
    def _delete_stale_positions(
        conn: sqlite3.Connection, agent_id: str, open_positions: dict[str, float]
    ) -> None:
        if not open_positions:
            conn.execute("DELETE FROM positions WHERE agent_id = ?", (agent_id,))
            return
        placeholders = ", ".join("?" * len(open_positions))
        conn.execute(
            f"DELETE FROM positions WHERE agent_id = ? AND product_id NOT IN ({placeholders})",
            (agent_id, *open_positions),
        )
