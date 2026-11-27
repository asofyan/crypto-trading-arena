"""Operator CLI for the arena bot: ``start``, ``stop``, ``status``, ``live-*``.

``start backend`` launches the tools/dashboard worker detached (log in ``.run/bot.log``);
``start tui`` runs the same worker in the foreground with the Rich dashboard attached.
``stop`` signals only a pid it can prove belongs to the bot, so a recycled pid in a stale
pid file can never take down an unrelated process. ``status`` reports the process plus the
wallet state the SQLite ledger has persisted, which is how an operator sees that a restart
resumed the previous session instead of resetting every agent to ``$100``, and whether the
engine hit a persistence failure (memory and disk diverged) that outlived the process.

Live trading adds four commands, split by who is allowed to write the ledger:

* ``live-init`` (engine stopped) declares which slice of the wallet the arena owns and
  records the go-live baseline, so pre-existing holdings are never counted as drift.
* ``reconcile`` (read-only) compares the wallet with the ledger, safe while the engine runs.
* ``halt`` (any time) writes the ``data/LIVE_HALT`` flag the risk gates consult per order.
* ``resume`` (engine stopped) clears that flag; the engine reconciles again before arming.

    uv run python manage_bot.py start backend
    uv run python manage_bot.py start tui --fresh
    uv run python manage_bot.py status --json
    uv run python manage_bot.py stop
    uv run python manage_bot.py live-init --fund 95 --agent a1
    uv run python manage_bot.py reconcile
    uv run python manage_bot.py halt --reason "checking something"
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from arena.ledger import FUTURES_LEDGER_META_KEY, Ledger, LedgerInUseError, LedgerSchemaError
from arena.live_session import HALTED, RECONCILING, REASON_META_KEY, STATE_META_KEY
from arena.models import AgentAccount
from arena.reconcile import compare
from arena.risk import DEFAULT_HALT_PATH

MANAGED_MODULE = "deploy.tools_and_dashboard"
RUN_DIR = Path(".run")
PID_FILE = RUN_DIR / "bot.pid"
LOG_FILE = RUN_DIR / "bot.log"
DEFAULT_DB = Path("data/arena.db")
ARCHIVE_DIRNAME = "archive"
# Everything one ledger owns on disk: the database plus its WAL and lock sidecars.
LEDGER_SIDECARS = ("", "-wal", "-shm", ".lock")

# Grace periods follow the documented escalation: SIGINT (recorder/ledger flush),
# then SIGTERM, then SIGKILL only when the operator explicitly passes --force.
SIGINT_GRACE_SECONDS = 15.0
SIGTERM_GRACE_SECONDS = 5.0
# Long enough to outlive the fast synchronous startup failures (bad config, import
# errors, a refused Kafka bootstrap) so `start` reports them with the log tail
# instead of claiming success on a child that is already dying.
STARTUP_PROBE_SECONDS = 4.0
LOG_TAIL_LINES = 20
_POLL_INTERVAL = 0.05


# ── Pid file ─────────────────────────────────────────────────────


def read_pidfile(path: Path | None = None) -> dict | None:
    """Parsed pid-file payload, or ``None`` when absent, unreadable, or malformed."""
    path = path or PID_FILE
    try:
        info = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
        return None
    return info


def write_pidfile(info: dict, path: Path | None = None) -> None:
    """Publish the pid file atomically, so a reader never parses a half-written file."""
    path = path or PID_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(info, indent=2) + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def remove_pidfile(path: Path | None = None, *, expect_pid: int) -> bool:
    """Delete the pid file iff it still names ``expect_pid``; ``True`` when removed.

    ``expect_pid`` is mandatory by design: a cleanup path that owns exactly one process
    must not unlink a pid file another (possibly live) process just published — that
    would leave a running engine with nothing for ``stop`` to find. The single caller
    that has already proved the named pid is dead but cannot read the payload (a
    malformed file) uses :func:`_unlink_pidfile_forced`.
    """
    path = path or PID_FILE
    info = read_pidfile(path)
    if info is None or info["pid"] != expect_pid:
        return False
    return _unlink_pidfile(path)


def _unlink_pidfile(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _unlink_pidfile_forced(path: Path | None = None) -> bool:
    """Delete the pid file with no ownership check; ``True`` when one was removed.

    Reserved for callers that already proved the pid it names is gone (typically by
    salvaging it out of a payload ``read_pidfile`` could not parse) and that therefore
    have no readable owner left to pass as ``expect_pid``.
    """
    return _unlink_pidfile(path or PID_FILE)


_SALVAGEABLE_PID = re.compile(r'"pid"\s*:\s*(\d+)')


def salvage_pid(contents: str) -> int | None:
    """Best-effort pid from a pid file that ``read_pidfile`` could not parse.

    A truncated write or an almost-valid payload still names the process that wrote
    it, and that pid is the whole difference between "safe to delete" and "an orphan
    nobody can stop". A missing or nonsensical pid stays ``None`` (fail closed).
    """
    match = _SALVAGEABLE_PID.search(contents)
    if match is None:
        return None
    pid = int(match.group(1))
    return pid if pid > 0 else None


def salvaged_live_pid(path: Path | None = None) -> int | None:
    """A live pid recovered from a pid file that ``read_pidfile`` rejects.

    ``start`` treats an unparsable pid file as "no engine" and overwrites it, which
    strands a live engine exactly the way deleting the file would. Publishing is the
    one write path, so the owner check has to happen here too: only a proved-dead
    (or unnamed) pid may be replaced.
    """
    path = path or PID_FILE
    try:
        contents = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    pid = salvage_pid(contents)
    return pid if pid is not None and pid_alive(pid) else None


# ── Process inspection ───────────────────────────────────────────


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` exists — one ``kill(0)`` syscall, no ``ps`` spawn.

    This is the probe the polling loops hammer, so it stays cheap. A zombie still
    answers signal 0; ``pid_zombie`` is the one-shot check for "exited, not reaped".
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def pid_zombie(pid: int) -> bool:
    """Whether ``pid`` is dead but still waiting for its parent to reap it."""
    if pid <= 0:
        return False
    return (process_stat(pid) or "").upper().startswith("Z")


def process_stat(pid: int) -> str | None:
    return _ps(pid, "stat=")


def process_command(pid: int) -> str | None:
    return _ps(pid, "command=")


def _ps(pid: int, field: str) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", field, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_managed_process(pid: int) -> bool:
    """True only when ``pid``'s command line is the bot module — the kill guard.

    Defaults to ``False`` when the command line cannot be read, so an unverifiable
    pid is never signalled.
    """
    command = process_command(pid)
    return bool(command) and MANAGED_MODULE in command


# ── Wallet (SQLite ledger) ───────────────────────────────────────


def resolve_db(explicit: str | Path | None, info: dict | None = None) -> Path:
    """Ledger for ``status``: explicit ``--db`` > the pid file's ``--db`` > default.

    A backend started with a custom ``--db`` must be inspectable with a bare
    ``status``, otherwise the operator reads the wrong wallet and thinks the
    restart reset everyone.
    """
    if explicit:
        return Path(explicit)
    recorded = _recorded_db(info)
    return Path(recorded) if recorded else DEFAULT_DB


def _recorded_db(info: dict | None) -> str | None:
    """The ``--db`` value from the engine command line recorded in the pid file."""
    cmd = (info or {}).get("cmd")
    if not isinstance(cmd, list):
        return None
    for index, token in enumerate(cmd):
        if token == "--db" and index + 1 < len(cmd):
            return str(cmd[index + 1])
        if isinstance(token, str) and token.startswith("--db="):
            return token.split("=", 1)[1]
    return None


def wallet_state(db: Path | str | None = None) -> dict:
    """Per-agent wallet rows for ``status``; empty when the ledger has no state yet.

    Read-only on purpose: ``status`` runs next to a live engine, and opening the
    ledger for writing would fence that engine out of its own database.
    """
    path = Path(db) if db is not None else DEFAULT_DB
    state: dict = {"db": str(path), "agents": [], "error": None, "degraded": None}
    if not path.exists():
        return state

    try:
        with Ledger(path, read_only=True) as ledger:
            state["agents"] = ledger.summary()
            state["degraded"] = ledger.degraded_marker()
            state["live"] = {
                "state": ledger.get_meta(STATE_META_KEY),
                "reason": ledger.get_meta(REASON_META_KEY) or "",
                "orders": ledger.order_counts(),
                "has_baseline": bool(
                    ledger.get_meta("live_baseline") or ledger.get_meta(FUTURES_LEDGER_META_KEY)
                ),
                "halt_flag": DEFAULT_HALT_PATH.exists(),
            }
    except FileNotFoundError:
        return state  # raced with --fresh archiving the database away
    except (LedgerInUseError, LedgerSchemaError, sqlite3.Error, OSError) as exc:
        state["error"] = str(exc)
    return state


# ── Status rendering ─────────────────────────────────────────────


def _format_uptime(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = int(max(seconds, 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def _parse_iso(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def process_state(now: datetime | None = None, info: dict | None = None) -> dict:
    info = read_pidfile() if info is None else info
    state: dict = {
        "state": "stopped",
        "pid": None,
        "mode": None,
        "started_at": None,
        "uptime": None,
        "uptime_seconds": None,
        "verified": False,
        "cmd": None,
        "no_ledger": False,
        "log": str(LOG_FILE),
    }
    if info is None:
        return state

    pid = info["pid"]
    state.update(
        pid=pid,
        mode=info.get("mode"),
        started_at=info.get("started_at"),
        cmd=info.get("cmd"),
        no_ledger=bool(info.get("no_ledger")),
        log=info.get("log") or state["log"],
    )
    # A zombie answers kill(0) but will never trade again; its parent simply has
    # not reaped it yet, so one `ps` call here settles it.
    if not pid_alive(pid) or pid_zombie(pid):
        state["state"] = "stale"
        return state

    state["state"] = "running"
    state["verified"] = is_managed_process(pid)
    started = _parse_iso(info.get("started_at"))
    if started is not None:
        elapsed = max(((now or datetime.now()) - started).total_seconds(), 0.0)
        state["uptime_seconds"] = round(elapsed, 1)
        state["uptime"] = _format_uptime(elapsed)
    return state


def _trading_mode(config_path: str | None) -> str | None:
    """Best-effort: a broken config must not stop ``status`` from reporting the wallet."""
    if not config_path:
        return None
    try:
        from config import load_config

        return load_config(config_path).trading.mode
    except Exception:  # noqa: BLE001
        return None


def build_status(
    db: Path | str | None = None, now: datetime | None = None, config_path: str | None = None
) -> dict:
    """Machine-readable snapshot consumed by ``render_status`` and ``status --json``."""
    info = read_pidfile()
    return {
        "process": process_state(now=now, info=info),
        "wallet": wallet_state(resolve_db(db, info)),
        "mode": _trading_mode(config_path),
    }


def _short_ts(value: str | None) -> str:
    return value.replace("T", " ")[:19] if value else "—"


def render_status(status: dict) -> str:
    proc = status["process"]
    lines: list[str] = []

    if proc["state"] == "running":
        detail = f"pid {proc['pid']}, mode {proc['mode']}"
        if proc["uptime"]:
            detail += f", up {proc['uptime']}"
        lines.append(f"Bot:     running ({detail})")
        if not proc["verified"]:
            lines.append(
                f"         unverified: 'ps' command line for pid {proc['pid']} does not "
                f"mention {MANAGED_MODULE}"
            )
    elif proc["state"] == "stale":
        lines.append(f"Bot:     not running (stale pid file for pid {proc['pid']}; run stop)")
    else:
        lines.append("Bot:     not running")
    lines.append(f"Log:     {proc['log']}")

    wallet = status["wallet"]
    lines.append("")
    if proc["no_ledger"]:
        # Nothing is persisted, so a wallet read would only show an unrelated file.
        lines.append("Wallet:  persistence disabled (started with --no-ledger)")
        return "\n".join(lines)
    marker = wallet.get("degraded")
    if marker:
        lines.append(
            f"WARNING: persistence degraded at {_short_ts(marker.get('ts'))} "
            f"({marker.get('agent')}) — fills after that point may diverge from disk"
        )
    if wallet["error"]:
        lines.append(f"Wallet:  unreadable ledger {wallet['db']}")
        lines.append(f"         {wallet['error']}")
        return "\n".join(lines)
    if not wallet["agents"]:
        lines.append(f"Wallet:  no wallet state yet ({wallet['db']})")
        live = wallet.get("live")
        if live is not None:
            lines.append("")
            lines.extend(render_live_status(live, status.get("mode")))
        return "\n".join(lines)

    lines.append(f"Wallet:  {wallet['db']}")
    lines.append(
        f"{'agent':<22}{'cash':>14}{'pos':>5}{'trades':>8}{'realized':>13}{'fees':>10}  last fill"
    )
    for row in wallet["agents"]:
        lines.append(
            f"{row['agent_id']:<22}{row['cash']:>14,.2f}{row['position_count']:>5}"
            f"{row['trade_count']:>8}{row['realized_pnl']:>13,.2f}"
            f"{row['total_fees_paid']:>10,.2f}  {_short_ts(row['last_fill_ts'])}"
        )

    live = wallet.get("live")
    if live is not None:
        lines.append("")
        lines.extend(render_live_status(live, status.get("mode")))
    return "\n".join(lines)


def render_live_status(live: dict, mode: str | None = None) -> list[str]:
    """The live-trading lines of ``status``: armed or not, and why."""
    if mode == "paper":
        # A paper ledger can carry live bookkeeping state (a halt test, a resume); the
        # honest answer to "is this trading real money?" is the configured mode.
        return ["Live:    not in use (trading.mode = paper)"]

    state = live.get("state")
    if not state and not live.get("orders") and not live.get("halt_flag"):
        return ["Live:    never armed (no live session recorded in this ledger)"]

    lines = []
    if live.get("halt_flag"):
        # The flag outranks the recorded state: a halt set while the engine was running
        # could not write the ledger, so the file is the newer truth.
        lines.append("Live:    HALTED — data/LIVE_HALT is set")
    elif state == HALTED:
        lines.append(f"Live:    HALTED — {live.get('reason') or 'no reason recorded'}")
    elif state == RECONCILING:
        lines.append("Live:    RECONCILING — restart the engine to finish arming")
    elif state:
        lines.append(f"Live:    {state}" + (f" — {live['reason']}" if live.get("reason") else ""))
    else:
        lines.append("Live:    unknown (session state missing)")

    orders = live.get("orders") or {}
    if orders:
        summary = ", ".join(f"{count} {state.lower()}" for state, count in sorted(orders.items()))
        lines.append(f"Orders:  {summary}")
    if not live.get("has_baseline"):
        lines.append(
            "         no go-live baseline recorded; live-init is required for spot. "
            "Live futures binds its ledger at startup."
        )
    if state == HALTED or live.get("halt_flag"):
        lines.append("         re-arm: `manage_bot.py stop && manage_bot.py resume`")
    return lines


# ── Launch plumbing ──────────────────────────────────────────────


def build_command(
    *,
    headless: bool,
    bootstrap_servers: str,
    config: str,
    data_dir: str,
    db: str,
    no_ledger: bool = False,
    snapshot_interval: float | None = None,
) -> list[str]:
    """The exact argv used for the child bot process (also recorded in the pid file)."""
    command = [
        sys.executable,
        "-m",
        MANAGED_MODULE,
        "--bootstrap-servers",
        bootstrap_servers,
        "--config",
        config,
        "--data-dir",
        data_dir,
        "--db",
        db,
    ]
    if no_ledger:
        command.append("--no-ledger")
    if headless:
        command.append("--headless")
    if snapshot_interval is not None:
        command.extend(["--snapshot-interval", str(snapshot_interval)])
    return command


def archive_ledger(db: Path | str | None = None) -> Path | None:
    """Move an existing wallet DB (plus WAL sidecars) into ``<db dir>/archive``.

    Archiving instead of deleting keeps history inspectable while letting ``--fresh``
    start everyone from ``INITIAL_CASH`` again.

    Raises ``LedgerInUseError`` when another process owns the single-writer lock: the
    move would leave a live engine writing to an unlinked file, so nothing is moved.
    """
    path = Path(db) if db is not None else DEFAULT_DB
    if not path.exists():
        _discard_orphan_sidecars(path)
        return None

    ledger: Ledger | None
    try:
        # Proves exclusivity, and keeps the lock held until the files have moved.
        ledger = Ledger(path)
    except LedgerInUseError:
        raise
    except (LedgerSchemaError, sqlite3.Error, OSError):
        # Corrupt or foreign file: --fresh exists to move exactly this aside, and the
        # writer lock was free (otherwise LedgerInUseError would have been raised).
        ledger = None

    try:
        if ledger is not None and ledger.get_meta(FUTURES_LEDGER_META_KEY) is not None:
            raise ValueError("cannot reset a bound futures ledger")
        target = _archive_dir(path) / (
            f"{path.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}{path.suffix}"
        )
        for suffix in LEDGER_SIDECARS:
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists():
                os.replace(sidecar, Path(f"{target}{suffix}"))
        return target
    finally:
        if ledger is not None:
            ledger.close()


def _archive_dir(db: Path) -> Path:
    """``<db dir>/archive``, created on demand."""
    directory = db.parent / ARCHIVE_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _lock_path(db: Path) -> Path:
    """Mirror of ``Ledger.lock_path`` — resolvable without opening the database."""
    return db.with_name(f"{db.name}.lock")


def _discard_orphan_sidecars(db: Path) -> None:
    """Drop ``-wal``/``-shm`` litter left behind by a database that is gone.

    The ``.lock`` file is never unlinked, not even when its flock looks free: a writer
    may have created it and be one syscall away from opening the database, and
    unlinking the inode it already holds would hand the next engine a fresh lock file —
    two writers, one ledger. A stale lock file is harmless, so the sweep only reports
    when one is actually held.
    """
    for suffix in ("-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)

    lock_path = _lock_path(db)
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LedgerInUseError(
                f"{lock_path}: another engine already owns this ledger (single-writer "
                f"lock is held). Stop it first."
            ) from exc
    finally:
        os.close(fd)


def tail_log(lines: int = LOG_TAIL_LINES) -> str:
    try:
        content = LOG_FILE.read_text(errors="replace").splitlines()
    except OSError as exc:
        return f"(no log at {LOG_FILE}: {exc})"
    return "\n".join(content[-lines:]) or "(log is empty)"


def _pid_entry(
    pid: int, mode: str, command: list[str], log: Path, *, no_ledger: bool = False
) -> dict:
    return {
        "pid": pid,
        "mode": mode,
        "started_at": datetime.now().isoformat(),
        "cmd": command,
        "log": str(log),
        "no_ledger": no_ledger,
    }


# ── Commands ─────────────────────────────────────────────────────


def cmd_start(args: argparse.Namespace) -> int:
    existing = read_pidfile()
    if existing is not None:
        if pid_alive(existing["pid"]):
            print(
                f"error: bot already running (pid {existing['pid']}, "
                f"mode {existing.get('mode')}). Stop it first: python manage_bot.py stop"
            )
            return 1
        print(f"note: removed stale pid file for dead pid {existing['pid']}")
        remove_pidfile(expect_pid=existing["pid"])
    else:
        stranded = salvaged_live_pid()
        if stranded is not None:
            print(
                f"error: {PID_FILE} is unreadable but names live pid {stranded}; "
                "refusing to overwrite it."
            )
            print(
                "       If that is an arena engine, this starts a second writer: run "
                "'python manage_bot.py stop' first, or remove the pid file by hand."
            )
            return 1

    db = Path(args.db) if args.db else DEFAULT_DB
    if args.fresh and args.no_ledger:
        # A volatile engine never opens the DB, so archiving here would destroy history
        # that this run is not going to replace.
        print(
            "note: --fresh ignored because --no-ledger is set "
            "(a volatile engine never reads the ledger)"
        )
    elif args.fresh:
        try:
            archived = archive_ledger(db)
        except LedgerInUseError as exc:
            print(f"error: refusing --fresh for {db}: {exc}")
            print("       Nothing was moved; stop the running engine first.")
            return 1
        except ValueError as exc:
            print(f"error: refusing --fresh for {db}: {exc}")
            return 1
        except OSError as exc:
            # Unmapped now that the ledger no longer reports filesystem failures as
            # "another engine owns it": surface it as a clean CLI error, not a traceback.
            print(f"error: cannot archive {db}: {exc}")
            print("       Nothing was moved; fix the filesystem and retry.")
            return 1
        if archived is not None:
            print(f"Archived previous wallet → {archived}")
        else:
            print(f"No existing wallet at {db}; starting from scratch")

    # The engine can only report a taken ledger through its log, by which point the
    # operator just sees a dying process. Probe first so the message is immediate.
    if not args.no_ledger and db.exists() and _ledger_owned_elsewhere(db):
        print(
            f"error: another engine already holds {db} (single-writer lock is taken). "
            f"Stop it first: python manage_bot.py stop"
        )
        return 1

    command = build_command(
        headless=args.mode == "backend",
        bootstrap_servers=args.bootstrap_servers,
        config=args.config,
        data_dir=args.data_dir,
        db=str(db),
        no_ledger=args.no_ledger,
        snapshot_interval=args.snapshot_interval,
    )
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    if args.mode == "tui":
        return _start_foreground(command, no_ledger=args.no_ledger)

    log_path = LOG_FILE
    with log_path.open("a", encoding="utf-8") as log_handle:
        log_handle.write(f"\n=== start {datetime.now().isoformat()} ===\n")
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # survive the launching shell / SSH session
        )
    try:
        write_pidfile(
            _pid_entry(process.pid, args.mode, command, log_path, no_ledger=args.no_ledger)
        )
    except OSError as exc:
        # Without a pid file nothing records this child: no `status`, no `stop`.
        _stop_untracked_child(
            process.pid,
            wait=lambda timeout: _wait_for_popen(process, timeout),
            label=f"backend (pid {process.pid})",
        )
        print(f"error: cannot write pid file {PID_FILE}: {exc}")
        print("       Stopped the just-launched backend; nothing is left running.")
        return 1
    print(f"Started backend (pid {process.pid}); logging to {log_path}")

    time.sleep(STARTUP_PROBE_SECONDS)
    returncode = process.poll()
    if returncode is not None:
        remove_pidfile(expect_pid=process.pid)
        print(f"error: backend exited within {STARTUP_PROBE_SECONDS:.1f}s (code {returncode})")
        print(f"Last {LOG_TAIL_LINES} log lines:")
        print(tail_log())
        return 1

    print("OK: process is alive; run 'python manage_bot.py status' to inspect the wallet.")
    return 0


def _ledger_owned_elsewhere(path: Path) -> bool:
    """Whether a foreign process holds ``path``'s single-writer lock."""
    try:
        Ledger(path).close()
    except LedgerInUseError:
        return True
    except (LedgerSchemaError, sqlite3.Error, OSError):
        # Unopenable for another reason (corrupt/foreign file): let the child report it
        # with open_ledger_or_exit's precise path+cause message instead of guessing.
        return False
    return False


def _start_foreground(command: list[str], *, no_ledger: bool = False) -> int:
    process = subprocess.Popen(command)  # inherits the tty: dashboard + Ctrl-C
    try:
        write_pidfile(_pid_entry(process.pid, "tui", command, LOG_FILE, no_ledger=no_ledger))
    except OSError as exc:
        # Same untracked-child window as the backend branch: no pid file, no `stop`.
        _stop_untracked_child(
            process.pid,
            wait=lambda timeout: _wait_for_popen(process, timeout),
            label=f"TUI (pid {process.pid})",
        )
        print(f"error: cannot write pid file {PID_FILE}: {exc}")
        print("       Stopped the just-launched TUI; nothing is left running.")
        return 1
    print(f"Started TUI (pid {process.pid}); Ctrl-C to stop.")
    try:
        returncode = process.wait()
    except KeyboardInterrupt:
        print("\nSIGINT received — stopping the TUI process...")
    else:
        remove_pidfile(expect_pid=process.pid)
        return returncode

    # The child may still be trading, so the pid file only goes away once the process
    # provably has: an orphaned engine with no pid file is worse than a stale pid file.
    code = _terminate(
        process.pid,
        wait=lambda timeout: _wait_for_popen(process, timeout),
        allow_sigkill=False,
        label=f"TUI (pid {process.pid})",
    )
    if code in (0, 1):
        remove_pidfile(expect_pid=process.pid)
        return 130
    print(f"Pid file {PID_FILE} kept so 'stop --force' can still reach pid {process.pid}.")
    return code


def _signal(pid: int, sig: int) -> bool:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        print(f"error: not permitted to signal pid {pid}")
        return False
    return True


def _stop_untracked_child(pid: int, *, wait: Callable[[float], bool], label: str) -> int:
    """Escalate until a child we failed to publish a pid file for is gone.

    SIGKILL is allowed from the start: with no pid file, "stop it later" is not an
    option, so the only alternatives are a graceful stop now or a permanent orphan.
    """
    return _terminate(pid, wait=wait, allow_sigkill=True, label=label)


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """Poll with ``kill(0)`` and settle the zombie question with one ``ps`` call."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(_POLL_INTERVAL)
    return not pid_alive(pid) or pid_zombie(pid)


def _wait_for_popen(process: subprocess.Popen, timeout: float) -> bool:
    """Wait for our own child (reaping it); ``True`` once it has exited."""
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _terminate(
    pid: int,
    *,
    wait: Callable[[float], bool],
    allow_sigkill: bool,
    label: str,
    verify: Callable[[], bool] | None = None,
) -> int:
    """Escalate SIGINT → SIGTERM → (only when allowed) SIGKILL.

    Returns ``0`` once the process is gone, ``1`` when it vanished before the first
    signal landed *or* ``verify`` reported that the pid is no longer ours, and ``2`` when
    it survived everything we were allowed to send. The caller owns the pid file:
    deleting it on ``2`` would orphan a live engine.

    ``verify`` re-checks ownership before every escalation. It matters because the pid
    file was written minutes ago: after a 15s grace window the pid may belong to an
    unrelated process, and SIGTERM/SIGKILL cannot be taken back.
    """
    print(f"Sending SIGINT to {label} — graceful shutdown...")
    if not _signal(pid, signal.SIGINT):
        print("Process disappeared before the signal landed.")
        return 1
    if wait(SIGINT_GRACE_SECONDS):
        return 0

    if verify is not None and not verify():
        print(f"error: pid {pid} is no longer {MANAGED_MODULE}; refusing to signal it again.")
        return 1
    print(f"No exit after {SIGINT_GRACE_SECONDS:.0f}s — sending SIGTERM...")
    _signal(pid, signal.SIGTERM)
    if wait(SIGTERM_GRACE_SECONDS):
        return 0

    if not allow_sigkill:
        print(
            f"error: pid {pid} is still alive; re-run 'python manage_bot.py stop --force' "
            f"to SIGKILL it. Pid file left in place."
        )
        return 2

    if verify is not None and not verify():
        print(f"error: pid {pid} is no longer {MANAGED_MODULE}; refusing to SIGKILL it.")
        return 1
    print("Sending SIGKILL (--force)...")
    _signal(pid, signal.SIGKILL)
    if wait(SIGTERM_GRACE_SECONDS):
        return 0

    print(f"error: pid {pid} survived SIGKILL; pid file left in place.")
    return 2


def cmd_stop(args: argparse.Namespace) -> int:
    info = read_pidfile()
    if info is None:
        if not PID_FILE.exists():
            print("Bot is not running (no pid file).")
            return 1
        return _stop_unreadable_pidfile()

    pid = info["pid"]
    mode = info.get("mode") or "?"
    if not pid_alive(pid):
        remove_pidfile(expect_pid=pid)
        print(f"Stale pid file for pid {pid} (process is gone); cleaned up.")
        return 1
    if not is_managed_process(pid):
        remove_pidfile(expect_pid=pid)
        print(
            f"Refusing to signal pid {pid}: its command line does not mention "
            f"{MANAGED_MODULE}. Removed the stale pid file instead."
        )
        return 1

    code = _terminate(
        pid,
        wait=lambda timeout: _wait_for_exit(pid, timeout),
        allow_sigkill=args.force,
        label=f"{mode} (pid {pid})",
        # Re-checked before SIGTERM and SIGKILL: pids get recycled during the grace
        # window, and an over-escalated signal cannot be undone.
        verify=lambda: is_managed_process(pid),
    )
    if code in (0, 1):
        remove_pidfile(expect_pid=pid)
    if code == 0:
        print(f"Stopped (pid {pid}).")
    return code


def _stop_unreadable_pidfile() -> int:
    """A pid file we cannot parse is not evidence that no engine is running.

    ``read_pidfile`` also returns ``None`` while another process is publishing, when
    the file's permissions changed, or for a truncated payload — deleting it in any of
    those cases is exactly how a live engine loses the only handle ``stop`` has on it.
    So: salvage the pid from the raw text and remove the file only once that pid is
    provably gone; otherwise fail closed and let the operator look.
    """
    try:
        contents = PID_FILE.read_text()
    except (OSError, UnicodeDecodeError):
        contents = ""
    pid = salvage_pid(contents)
    if pid is not None and not pid_alive(pid):
        _unlink_pidfile_forced()
        print(f"Removed unreadable pid file {PID_FILE} (pid {pid} is gone); nothing to stop.")
        return 1
    print(
        f"error: unreadable pid file {PID_FILE}; remove it manually if no engine is running."
    )
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    status = build_status(args.db, config_path=args.config)
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(render_status(status))
    return 0


# ── CLI ──────────────────────────────────────────────────────────


# ── Live trading controls ────────────────────────────────────────


def _require_stopped(action: str) -> bool:
    """Live-state writes need the ledger's writer lock, which a running engine holds."""
    state = process_state(info=read_pidfile())
    if state["state"] == "running":
        print(
            f"error: the bot is running (pid {state['pid']}); `{action}` needs the wallet "
            f"ledger to itself.\n  Run `python manage_bot.py stop` first."
        )
        return False
    return True


def _live_config_or_exit(config_path: str):
    from config import load_config_strict

    try:
        cfg = load_config_strict(config_path)
    except Exception as exc:  # noqa: BLE001 — the loaders already explain themselves
        print(f"error: cannot load {config_path}: {exc}")
        raise SystemExit(1) from exc
    if cfg.trading.mode != "live":
        print(
            f"error: {config_path} sets trading.mode = {cfg.trading.mode!r}; the live "
            f"commands need 'live'"
        )
        raise SystemExit(1)
    return cfg


def _venue_client_or_exit(cfg):
    from exchanges.binance_spot import (
        BINANCE_SPOT_REST,
        BINANCE_SPOT_TESTNET_REST,
        BinanceSpotClient,
    )

    live = cfg.trading.live
    if live.market == "perp":
        print("Live futures initializes and reconciles in the tool server. "
              "Use status with the futures --db to read its persisted state.")
        raise SystemExit(1)
    account_name = sorted(set(live.agent_accounts.values()))[0]
    account = live.accounts[account_name]
    base_url = BINANCE_SPOT_TESTNET_REST if account.testnet else BINANCE_SPOT_REST
    client = BinanceSpotClient(
        account.api_key,
        account.api_secret,
        base_url=base_url,
        recv_window_ms=live.recv_window_ms,
    )
    try:
        client.sync_time()
        info = client.exchange_info(sorted(live.allow_symbols))
        client.remember_symbols(info)
    except Exception as exc:  # noqa: BLE001 — credentials/network/venue
        client.close()
        print(f"error: cannot reach {base_url}: {exc}")
        raise SystemExit(1) from exc
    return client, account_name, info


def cmd_halt(args: argparse.Namespace) -> int:
    path = Path(args.flag)
    path.parent.mkdir(parents=True, exist_ok=True)
    reason = args.reason or "operator halt"
    path.write_text(reason + "\n", encoding="utf-8")
    print(f"Live trading halted: {reason}")
    print(f"  flag:    {path}")
    print("  effect:  a running engine refuses the next order (checked per order)")
    print("  re-arm:  python manage_bot.py stop && python manage_bot.py resume")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    if not _require_stopped("resume"):
        return 1
    flag = Path(args.flag)
    info = read_pidfile()
    db = resolve_db(args.db, info)
    if not flag.exists() and not db.exists():
        print("nothing to resume: no halt flag and no ledger")
        return 0

    with Ledger(db) as ledger:
        # The recorded state is what survives; clearing the flag is the last step, so a
        # crash in between leaves trading disarmed rather than secretly armed.
        ledger.set_meta(STATE_META_KEY, RECONCILING)
        ledger.set_meta(REASON_META_KEY, "")
    if flag.exists():
        flag.unlink()
    print("Halt cleared. The next start reconciles against the venue and arms itself")
    print("only if the wallet and the ledger agree — otherwise it stays HALTED.")
    print("  next: python manage_bot.py start backend")
    return 0


def _ledger_view(db: Path) -> tuple[dict[str, float], float, dict[str, float], dict[str, str]]:
    """Read the baseline, allocated cash, positions and agents from a stopped-or-running DB."""
    with Ledger(db, read_only=True) as ledger:
        raw_baseline = ledger.get_meta("live_baseline")
        accounts = ledger.load_accounts()
    baseline: dict[str, float] = {}
    if raw_baseline:
        try:
            baseline = {str(k): float(v) for k, v in json.loads(raw_baseline).items()}
        except (ValueError, TypeError):
            baseline = {}
    return baseline, accounts


def cmd_reconcile(args: argparse.Namespace) -> int:
    from arena.instruments import parse_exchange_info

    cfg = _live_config_or_exit(args.config)
    client, account_name, info = _venue_client_or_exit(cfg)
    try:
        rules = parse_exchange_info(info)
        balances = {asset: balance.total for asset, balance in client.balances().items()}
        open_orders = len(client.open_orders())
    finally:
        client.close()

    db = resolve_db(args.db, read_pidfile())
    if not db.exists():
        print(f"error: no ledger at {db}; run `live-init` after the first start")
        return 1

    with Ledger(db, read_only=True) as ledger:
        raw_baseline = ledger.get_meta("live_baseline")
        accounts = ledger.load_accounts()
    baseline = {}
    if raw_baseline:
        baseline = {str(k): float(v) for k, v in json.loads(raw_baseline).items()}

    mapped = [
        agent
        for agent, mapped_account in cfg.trading.live.agent_accounts.items()
        if mapped_account == account_name
    ]
    positions: dict[str, float] = {}
    cash = 0.0
    for agent in mapped:
        account = accounts.get(agent)
        if account is None:
            continue
        cash += account.cash
        for symbol, quantity in account.positions.items():
            positions[symbol] = positions.get(symbol, 0.0) + quantity

    report = compare(
        balances=balances,
        baseline=baseline,
        ledger_positions=positions,
        ledger_cash=cash,
        rules=rules,
        reserve_usdt=cfg.trading.live.reserve_usdt,
        open_orders=open_orders,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "armed": report.armed,
                    "blocking": [d.detail for d in report.blocking],
                    "notes": [d.detail for d in report.notes],
                    "balances": report.balances,
                    "baseline": report.baseline,
                    "ledger_cash": report.ledger_cash,
                    "ledger_positions": report.ledger_positions,
                },
                indent=2,
            )
        )
    else:
        print(f"Account: {account_name}  simulation: {'no' if args.json else 'read-only'}")
        print(report.summary())
        if not report.armed:
            print("\n=> a start would stay HALTED. Fix the difference, or accept a")
            print("   withdrawal with: python manage_bot.py reconcile --write-down <amount>")
    return 0 if report.armed else 1


def cmd_live_init(args: argparse.Namespace) -> int:
    if not _require_stopped("live-init"):
        return 1
    from arena.instruments import parse_exchange_info

    cfg = _live_config_or_exit(args.config)
    live = cfg.trading.live
    quote = "USDT"
    client, account_name, info = _venue_client_or_exit(cfg)
    try:
        balances = client.balances()
    finally:
        client.close()
    parse_exchange_info(info)  # fail early if the venue does not list our symbols

    mapped = [
        agent
        for agent, mapped_account in live.agent_accounts.items()
        if mapped_account == account_name
    ]
    agent = args.agent or (mapped[0] if len(mapped) == 1 else None)
    if agent is None:
        print(f"error: --agent is required when {len(mapped)} agents trade {account_name}")
        return 1

    free_quote = balances[quote].free if quote in balances else 0.0
    required = args.fund + live.reserve_usdt
    if required > free_quote + 1e-9:
        print(
            f"error: {quote} balance is {free_quote:,.2f}; fund {args.fund:,.2f} + reserve "
            f"{live.reserve_usdt:,.2f} needs {required:,.2f}"
        )
        return 1

    db = resolve_db(args.db, read_pidfile())
    with Ledger(db) as ledger:
        existing = ledger.load_accounts().get(agent)
        if existing and (existing.positions or existing.trade_count) and not args.force:
            print(
                f"error: ledger already has live history for {agent} "
                f"({existing.trade_count} fill(s), {len(existing.positions)} position(s))."
                "\n  Use a dedicated live ledger (--db data/live.db) or pass --force."
            )
            return 1

        # The baseline is everything the arena does *not* own: pre-existing base assets,
        # plus the quote that stays unallocated. Without it, the first reconcile would
        # report the funding itself as drift.
        baseline = {asset: balance.total for asset, balance in balances.items()}
        baseline[quote] = max(0.0, free_quote - args.fund - live.reserve_usdt)

        account = existing or AgentAccount()
        account.cash = args.fund
        account.positions.clear()
        account.cost_basis.clear()
        account.avg_entry_ts.clear()
        account.trade_count = 0
        account.total_fees_paid = 0.0
        account.realized_pnl = 0.0
        ledger.persist_account(agent, account)
        ledger.set_meta("live_baseline", json.dumps(baseline, sort_keys=True))
        ledger.set_meta("live_session_start_equity", repr(float(args.fund)))
        ledger.set_meta(STATE_META_KEY, RECONCILING)
        ledger.set_meta(REASON_META_KEY, "")
        ledger.record_equity_event(
            kind="initial_allocation",
            amount=args.fund,
            equity_before=0.0,
            equity_after=float(args.fund),
            agent_id=agent,
            note=f"{account_name}: {quote} {free_quote:,.2f} free, reserve {live.reserve_usdt:,.2f}",
        )

    print(f"Live ledger prepared: {db}")
    print(f"  agent:     {agent} → {account_name}")
    print(f"  allocated: {args.fund:,.2f} {quote} (reserve {live.reserve_usdt:,.2f})")
    print(f"  baseline:  {json.dumps(baseline, sort_keys=True)}")
    print("  next:      python manage_bot.py reconcile   (read-only check)")
    print("             then start the engine; it arms itself only if reconciled")
    return 0


def _add_launch_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bootstrap-servers", default="localhost:9092", help="Kafka bootstrap servers"
    )
    parser.add_argument("--config", default="config.json", help="Path to config file")
    parser.add_argument(
        "--data-dir", default="./data", help="Directory for CSV snapshots/trades"
    )
    parser.add_argument(
        "--db", default=None, help=f"SQLite wallet ledger path (default: {DEFAULT_DB})"
    )
    parser.add_argument(
        "--no-ledger",
        action="store_true",
        help="run without wallet persistence (volatile wallets)",
    )
    parser.add_argument(
        "--snapshot-interval",
        type=float,
        default=None,
        help=(
            "Seconds between portfolio snapshots (default: the engine's own 600). "
            "The read-only monitor builds its roster for idle agents from these, so a "
            "shorter interval is what makes a non-trading agent visible sooner."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_bot.py",
        description="Start, stop, and inspect the arena trading bot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python manage_bot.py start backend      # detached, logs in .run/bot.log\n"
            "  python manage_bot.py start tui --fresh  # foreground dashboard, new wallet\n"
            "  python manage_bot.py status --json\n"
            "  python manage_bot.py stop --force\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser(
        "start", help="start the bot worker (backend = detached, tui = foreground)"
    )
    start.add_argument(
        "mode",
        nargs="?",
        default="backend",
        choices=("backend", "tui"),
        help="backend: detached with logs in .run/bot.log (default); "
        "tui: foreground Rich dashboard",
    )
    start.add_argument(
        "--fresh",
        action="store_true",
        help="archive the existing wallet DB first, so every agent restarts at $100",
    )
    _add_launch_flags(start)
    start.set_defaults(handler=cmd_start)

    stop = subparsers.add_parser(
        "stop", help="stop the running bot (SIGINT → SIGTERM, SIGKILL only with --force)"
    )
    stop.add_argument(
        "--force", action="store_true", help="SIGKILL if SIGINT and SIGTERM do not stop it"
    )
    stop.set_defaults(handler=cmd_stop)

    halt = subparsers.add_parser(
        "halt", help="stop live trading immediately (a flag every order checks)"
    )
    halt.add_argument("--reason", default="", help="why (written into the flag file)")
    halt.add_argument("--flag", default=str(DEFAULT_HALT_PATH), help="halt flag path")
    halt.set_defaults(handler=cmd_halt)

    resume = subparsers.add_parser(
        "resume", help="clear a halt and let the next start re-reconcile and arm"
    )
    resume.add_argument("--db", default=None, help=f"SQLite wallet ledger (default: {DEFAULT_DB})")
    resume.add_argument("--flag", default=str(DEFAULT_HALT_PATH), help="halt flag path")
    resume.set_defaults(handler=cmd_resume)

    reconcile = subparsers.add_parser(
        "reconcile", help="compare the wallet with the ledger (read-only, safe while running)"
    )
    reconcile.add_argument("--config", default="config.json", help="Path to config file")
    reconcile.add_argument("--db", default=None, help=f"SQLite wallet ledger (default: {DEFAULT_DB})")
    reconcile.add_argument("--json", action="store_true", help="machine-readable report")
    reconcile.set_defaults(handler=cmd_reconcile)

    live_init = subparsers.add_parser(
        "live-init",
        help="declare the arena's slice of the wallet and record the go-live baseline",
    )
    live_init.add_argument("--fund", type=float, required=True, help="quote amount to allocate")
    live_init.add_argument("--agent", default=None, help="agent that will trade this account")
    live_init.add_argument("--config", default="config.json", help="Path to config file")
    live_init.add_argument("--db", default=None, help=f"SQLite wallet ledger (default: {DEFAULT_DB})")
    live_init.add_argument(
        "--force", action="store_true", help="reset an agent that already has live history"
    )
    live_init.set_defaults(handler=cmd_live_init)

    status = subparsers.add_parser("status", help="show process state and wallet balances")
    status.add_argument(
        "--db", default=None, help=f"SQLite wallet ledger path (default: {DEFAULT_DB})"
    )
    status.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    status.add_argument(
        "--config",
        default="config.json",
        help="Path to config file, used to report the trading mode",
    )
    status.set_defaults(handler=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
