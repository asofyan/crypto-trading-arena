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
from datetime import datetime, timezone
from pathlib import Path

from arena.models import AgentAccount, OrderRecord, TradeLogEntry

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/arena.db")
SCHEMA_VERSION = 2
RECENT_FILLS_LIMIT = 50
# meta row that makes a failed fill persist across processes: the TUI runs inside
# Live(screen=True), so a logged traceback is erased the moment the operator exits.
DEGRADED_META_KEY = "ledger_degraded"

# One row per submitted order, written *before* the venue is contacted. It is what
# makes a crash between "POST sent" and "fill booked" recoverable: on startup every
# row still in PENDING/SUBMITTED/UNKNOWN is resolved against the venue by
# ``client_order_id``, and the fill is applied at most once (see ``settle_fill``).
_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty_requested REAL,
    quote_qty_requested REAL,
    state TEXT NOT NULL,
    venue_order_id TEXT,
    executed_qty REAL,
    cum_notional REAL,
    fee REAL,
    fee_asset TEXT,
    price REAL,
    error TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_state ON orders(state);
-- Money that entered or left the wallet without a fill: a hand-made deposit adopted by
-- the reconciler, an explicit write-down after a withdrawal, the go-live baseline.
CREATE TABLE IF NOT EXISTS equity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent_id TEXT,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    equity_before REAL NOT NULL,
    equity_after REAL NOT NULL,
    note TEXT
);
"""

# Executed against every writer open. On a v1 file CREATE TABLE IF NOT EXISTS leaves
# the old `fills` shape alone — the columns are added by the 1→2 migration below — so
# the unique index cannot live here: it would name a column that does not exist yet.
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
    latency REAL,
    client_order_id TEXT,
    fee_asset TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- summary() asks for MAX(ts) per agent; without this it scans every fill.
CREATE INDEX IF NOT EXISTS fills_agent_ts ON fills(agent_id, ts);
""" + _ORDERS_DDL

# Runs after the version row is known to be 2, so the columns it references exist.
_V2_INDEXES = """
-- Partial: paper fills carry no client_order_id, and many NULLs are all "distinct".
CREATE UNIQUE INDEX IF NOT EXISTS fills_client_order_id ON fills(client_order_id)
    WHERE client_order_id IS NOT NULL;
"""

# The states a journal row may hold. Typos here would silently disable reconciliation,
# which is the one thing the journal exists for, so writes are validated.
ORDER_STATES = (
    "PENDING",          # written, venue not contacted yet
    "SUBMITTED",        # venue acknowledged, fill not booked yet
    "FILLED",
    "PARTIALLY_FILLED",
    "EXPIRED",          # venue accepted then killed it with nothing (or something) filled
    "REJECTED",         # definitive venue refusal, or refused locally by a risk gate
    "UNKNOWN",          # network error: only the venue knows, resolve on next start
    "DRY_RUN",          # live config, venue deliberately not contacted
)
_DANGLING_STATES = ("PENDING", "SUBMITTED", "UNKNOWN")

_ISO_WITH_SECONDS = "%H:%M:%S"


_ORDER_COLUMNS = (
    "client_order_id, ts, agent_id, account, symbol, side, qty_requested,"
    " quote_qty_requested, state, venue_order_id, executed_qty, cum_notional, fee,"
    " fee_asset, price, error, updated_at"
)


def _row_to_order(row: tuple) -> OrderRecord:
    return OrderRecord(*row)


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
            self._ensure_v2_indexes()
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

        if found == SCHEMA_VERSION:
            return

        if found is not None and found < SCHEMA_VERSION:
            if self.read_only:
                # Readers must not write, and a v1 file has no `orders` table or fill
                # order ids; only the engine (the single writer) can migrate it.
                self.close()
                raise LedgerSchemaError(
                    f"{self.path}: ledger schema {found} is older than {SCHEMA_VERSION}. "
                    f"Start the engine once (it migrates in place), then read it again."
                )
            self._migrate(found)
            return

        self.close()
        raise LedgerSchemaError(
            f"{self.path}: ledger schema version mismatch (found {row[0]!r}, "
            f"expected {SCHEMA_VERSION}). Refusing to open a database written by a "
            f"newer schema version — upgrade the code or move the file aside."
        )

    def _migrate(self, from_version: int) -> None:
        """Bring an older database up to ``SCHEMA_VERSION``, all-or-nothing.

        SQLite DDL is transactional, so a failure here leaves the file at its original
        version instead of half-migrated. The new tables already exist by this point:
        ``_SCHEMA`` runs ``CREATE TABLE IF NOT EXISTS`` on every writer open.
        """
        conn = self._db()
        with conn:
            if from_version == 1:
                conn.execute("ALTER TABLE fills ADD COLUMN client_order_id TEXT")
                conn.execute("ALTER TABLE fills ADD COLUMN fee_asset TEXT")
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
        log.info("ledger: migrated %s from schema %s to %s", self.path, from_version,
                 SCHEMA_VERSION)

    def _ensure_v2_indexes(self) -> None:
        conn = self._db()
        conn.executescript(_V2_INDEXES)
        conn.commit()

    @staticmethod
    def _validate_state(state: str) -> None:
        if state not in ORDER_STATES:
            raise ValueError(
                f"unknown order state {state!r}; expected one of {', '.join(ORDER_STATES)}"
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
            with conn:  # one transaction: account + positions + fill land together
                self._persist_account_state(conn, agent_id, account, timestamp)
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

    # ── Order journal (live execution) ───────────────────────────

    def _persist_account_state(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        account: AgentAccount,
        timestamp: str,
    ) -> None:
        """Mirror one account (cash, positions, counters) onto disk.

        Shared by the paper and the venue fill paths so the two can never disagree
        about what a booked fill looks like on disk.
        """
        # Snapshot before writing: another thread may mutate these mappings mid-loop
        # (a concurrent buy/sell on the same agent), which would otherwise raise
        # "dictionary changed size during iteration" or persist a torn state.
        positions = dict(account.positions)
        cost_basis = dict(account.cost_basis)
        avg_entry_ts = dict(account.avg_entry_ts)
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
                (agent_id, pid, qty, cost_basis.get(pid, 0.0), avg_entry_ts.get(pid)),
            )
        # A full sell drops the key from account.positions; mirror that on disk.
        self._delete_stale_positions(conn, agent_id, positions)

    def save_order(
        self,
        *,
        client_order_id: str,
        agent_id: str,
        account: str,
        symbol: str,
        side: str,
        state: str,
        qty_requested: float | None = None,
        quote_qty_requested: float | None = None,
        timestamp: str | None = None,
    ) -> None:
        """Journal the intent *before* the venue is contacted.

        A duplicate ``client_order_id`` raises ``sqlite3.IntegrityError`` on purpose:
        reusing an id would overwrite the only record of an order that may already have
        filled.
        """
        self._validate_state(state)
        now = timestamp or datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._db()
            with conn:
                conn.execute(
                    "INSERT INTO orders (client_order_id, ts, agent_id, account, symbol, side,"
                    " qty_requested, quote_qty_requested, state, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        client_order_id, now, agent_id, account, symbol, side,
                        qty_requested, quote_qty_requested, state, now,
                    ),
                )

    def update_order(
        self,
        client_order_id: str,
        *,
        state: str | None = None,
        venue_order_id: str | None = None,
        executed_qty: float | None = None,
        cum_notional: float | None = None,
        fee: float | None = None,
        fee_asset: str | None = None,
        price: float | None = None,
        error: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        """Record what the venue answered. Only the given fields are touched."""
        if state is not None:
            self._validate_state(state)
        updates: list[tuple[str, object]] = [
            ("state", state),
            ("venue_order_id", venue_order_id),
            ("executed_qty", executed_qty),
            ("cum_notional", cum_notional),
            ("fee", fee),
            ("fee_asset", fee_asset),
            ("price", price),
            ("error", error),
        ]
        assignments = [f"{column} = ?" for column, value in updates if value is not None]
        params = [value for _, value in updates if value is not None]
        assignments.append("updated_at = ?")
        params.append(timestamp or datetime.now(timezone.utc).isoformat())
        params.append(client_order_id)
        with self._lock:
            conn = self._db()
            with conn:
                conn.execute(
                    f"UPDATE orders SET {', '.join(assignments)} "
                    f"WHERE client_order_id = ?",
                    params,
                )

    def settle_fill(
        self,
        *,
        client_order_id: str,
        agent_id: str,
        account: AgentAccount,
        timestamp: str,
        action: str,
        product_id: str,
        quantity: float,
        price: float,
        fee: float,
        cash_after: float,
        fee_asset: str | None = None,
        latency: float | None = None,
        venue_order_id: str | None = None,
        executed_qty: float | None = None,
        cum_notional: float | None = None,
        state: str = "FILLED",
    ) -> bool:
        """Book a venue-confirmed fill and close its journal row, exactly once.

        Returns ``False`` when this ``client_order_id`` was already booked — a replay
        after a crash, or the same fill arriving twice. Nothing is double-counted: the
        fill row's partial unique index decides, in the same transaction that writes
        the account state.
        """
        self._validate_state(state)
        with self._lock:
            conn = self._db()
            with conn:
                cursor = conn.execute(
                    "INSERT INTO fills (ts, agent_id, action, product_id, quantity, price, fee,"
                    " cash_after, latency, client_order_id, fee_asset)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT DO NOTHING",
                    (
                        timestamp, agent_id, action, product_id, quantity, price, fee,
                        cash_after, latency, client_order_id, fee_asset,
                    ),
                )
                booked = cursor.rowcount > 0
                if booked:
                    self._persist_account_state(conn, agent_id, account, timestamp)
                self._close_order_row(
                    conn,
                    client_order_id,
                    state=state,
                    venue_order_id=venue_order_id,
                    executed_qty=executed_qty if executed_qty is not None else quantity,
                    cum_notional=cum_notional if cum_notional is not None else quantity * price,
                    fee=fee,
                    fee_asset=fee_asset,
                    price=price,
                )
        if not booked:
            log.warning(
                "ledger: fill for %s was already booked; replay ignored", client_order_id
            )
        return booked

    @staticmethod
    def _close_order_row(
        conn: sqlite3.Connection,
        client_order_id: str,
        *,
        state: str,
        venue_order_id: str | None,
        executed_qty: float | None,
        cum_notional: float | None,
        fee: float | None,
        fee_asset: str | None,
        price: float | None,
    ) -> None:
        conn.execute(
            "UPDATE orders SET state = ?, venue_order_id = COALESCE(?, venue_order_id),"
            " executed_qty = ?, cum_notional = ?, fee = ?, fee_asset = ?, price = ?,"
            " updated_at = ? WHERE client_order_id = ?",
            (
                state, venue_order_id, executed_qty, cum_notional, fee, fee_asset, price,
                datetime.now(timezone.utc).isoformat(), client_order_id,
            ),
        )

    def has_fill(self, client_order_id: str) -> bool:
        """Whether this order already booked a fill (checked *before* mutating memory)."""
        with self._lock:
            row = self._db().execute(
                "SELECT 1 FROM fills WHERE client_order_id = ? LIMIT 1", (client_order_id,)
            ).fetchone()
        return row is not None

    def get_order(self, client_order_id: str) -> OrderRecord | None:
        with self._lock:
            row = self._db().execute(
                f"SELECT {_ORDER_COLUMNS} FROM orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return _row_to_order(row) if row is not None else None

    def dangling_orders(self) -> list[OrderRecord]:
        """Orders whose outcome is still unknown to this process (oldest first)."""
        placeholders = ", ".join("?" * len(_DANGLING_STATES))
        with self._lock:
            rows = self._db().execute(
                f"SELECT {_ORDER_COLUMNS} FROM orders WHERE state IN ({placeholders})"
                f" ORDER BY ts",
                _DANGLING_STATES,
            ).fetchall()
        return [_row_to_order(row) for row in rows]

    def recent_orders(self, limit: int = 50) -> list[OrderRecord]:
        """Newest first, for the CLI and the dashboard."""
        with self._lock:
            rows = self._db().execute(
                f"SELECT {_ORDER_COLUMNS} FROM orders ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_order(row) for row in rows]

    def order_counts(self) -> dict[str, int]:
        """Journal rows per state — one query for ``manage_bot.py status``."""
        with self._lock:
            rows = self._db().execute(
                "SELECT state, COUNT(*) FROM orders GROUP BY state ORDER BY state"
            ).fetchall()
        return {state: count for state, count in rows}

    # ── Equity events (money in/out without a fill) ──────────────

    def record_equity_event(
        self,
        *,
        kind: str,
        amount: float,
        equity_before: float,
        equity_after: float,
        agent_id: str | None = None,
        note: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        with self._lock:
            conn = self._db()
            with conn:
                conn.execute(
                    "INSERT INTO equity_events (ts, agent_id, kind, amount, equity_before,"
                    " equity_after, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        timestamp or datetime.now(timezone.utc).isoformat(),
                        agent_id, kind, amount, equity_before, equity_after, note,
                    ),
                )

    def equity_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db().execute(
                "SELECT ts, agent_id, kind, amount, equity_before, equity_after, note"
                " FROM equity_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "ts": ts,
                "agent_id": agent_id,
                "kind": kind,
                "amount": amount,
                "equity_before": equity_before,
                "equity_after": equity_after,
                "note": note,
            }
            for ts, agent_id, kind, amount, equity_before, equity_after, note in rows
        ]

    # ── Generic meta ─────────────────────────────────────────────

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._db()
            with conn:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db().execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default
