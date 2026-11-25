"""Tests for the operator CLI (manage_bot.py): pid-file handling, start/stop, status.

Every test redirects ``PID_FILE``/``LOG_FILE``/``DEFAULT_DB`` into ``tmp_path``, and
``start`` is exercised through a monkeypatched ``build_command`` so no test ever
spawns the real bot (which would need a Kafka broker).
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import manage_bot
from arena.ledger import Ledger, LedgerInUseError
from arena.models import AgentAccount

REPO_ROOT = Path(__file__).resolve().parents[1]

# Writes one fill, then dies without closing the ledger: the WAL sidecars a killed
# engine leaves behind (which ``--fresh`` has to move along with the database).
CRASH_WRITER = """
import os, sys
from arena.ledger import Ledger
from arena.models import AgentAccount

ledger = Ledger(sys.argv[1])
ledger.save_fill(
    agent_id="crash-agent",
    account=AgentAccount(cash=99_000.0, trade_count=1, total_fees_paid=10.0),
    timestamp="2025-01-01T00:00:00",
    action="buy",
    product_id="BTC-USD",
    quantity=0.02,
    price=50_000.0,
    fee=10.0,
    cash_after=99_000.0,
    latency=0.5,
)
os._exit(1)
"""

# Child process standing in for the bot: reports readiness, then records which
# signal arrived before exiting cleanly.
STUB_SIGNAL_SCRIPT = """
import signal, sys, pathlib, time
marker = pathlib.Path(sys.argv[1])
cooperative = sys.argv[2] == "cooperative"

def _exit(signum, frame):
    marker.write_text(str(signum))
    sys.exit(0)

if cooperative:
    signal.signal(signal.SIGINT, _exit)
    signal.signal(signal.SIGTERM, _exit)
else:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

print("ready", flush=True)
while True:
    time.sleep(0.02)
"""


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """Redirect every on-disk artifact manage_bot touches into tmp_path."""
    run = tmp_path / ".run"
    monkeypatch.setattr(manage_bot, "PID_FILE", run / "bot.pid")
    monkeypatch.setattr(manage_bot, "LOG_FILE", run / "bot.log")
    monkeypatch.setattr(manage_bot, "DEFAULT_DB", tmp_path / "data" / "arena.db")
    monkeypatch.setattr(manage_bot, "DEFAULT_HALT_PATH", tmp_path / "data" / "LIVE_HALT")
    monkeypatch.setattr(manage_bot, "STARTUP_PROBE_SECONDS", 0.2)
    # Keep the escalation ladder fast; the ordering under test is unchanged.
    monkeypatch.setattr(manage_bot, "SIGINT_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(manage_bot, "SIGTERM_GRACE_SECONDS", 0.3)
    return run


def _pid_entry(pid: int, mode: str = "backend") -> dict:
    return {
        "pid": pid,
        "mode": mode,
        "started_at": datetime.now().isoformat(),
        "cmd": ["python", "-m", manage_bot.MANAGED_MODULE],
        "log": "bot.log",
    }


def _spawn_stub(tmp_path, *, managed: bool, cooperative: bool = True):
    """Spawn a stub bot process; ``managed`` embeds the module name in its argv."""
    marker = tmp_path / "marker"
    prefix = f"# {manage_bot.MANAGED_MODULE}\n" if managed else ""
    proc = subprocess.Popen(
        [sys.executable, "-c", prefix + STUB_SIGNAL_SCRIPT, str(marker),
         "cooperative" if cooperative else "stubborn"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc, marker


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


class _StubTui:
    """Popen stand-in for the foreground TUI: the first wait() models the operator's Ctrl-C."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._interrupted = False

    @property
    def pid(self) -> int:
        return self._proc.pid

    def wait(self, timeout: float | None = None):
        if not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        return self._proc.wait(timeout=timeout)

    def poll(self):
        return self._proc.poll()


def _patch_tui_spawn(monkeypatch, proc: subprocess.Popen) -> _StubTui:
    """Route only the TUI launch to an adapter over an already-running stub child.

    Everything else (notably the ``ps`` probes behind liveness checks) still reaches
    the real ``Popen``, so the test exercises the production escalation ladder.
    """
    sentinel = ["stub-tui"]
    stub = _StubTui(proc)
    real_popen = subprocess.Popen

    def fake_popen(*args, **kwargs):
        if args and args[0] == sentinel:
            return stub
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(manage_bot, "build_command", lambda **kwargs: sentinel)
    monkeypatch.setattr(manage_bot.subprocess, "Popen", fake_popen)
    return stub


# ── pid file + liveness primitives ───────────────────────────────


def test_pidfile_roundtrip(run_dir):
    info = _pid_entry(4242, "tui")
    manage_bot.write_pidfile(info)

    assert manage_bot.read_pidfile() == info
    assert manage_bot.remove_pidfile(expect_pid=4242) is True
    assert manage_bot.read_pidfile() is None
    assert manage_bot.remove_pidfile(expect_pid=4242) is False


def test_remove_pidfile_keeps_a_file_owned_by_another_pid(run_dir):
    """Cleanup must not unlink the pid file of a process it does not own."""
    owner = os.getpid()  # alive by construction
    manage_bot.write_pidfile(_pid_entry(owner))

    assert manage_bot.remove_pidfile(expect_pid=owner + 1) is False
    assert manage_bot.read_pidfile()["pid"] == owner

    assert manage_bot.remove_pidfile(expect_pid=owner) is True
    assert not manage_bot.PID_FILE.exists()


def test_read_pidfile_tolerates_corrupt_file(run_dir):
    run_dir.mkdir(parents=True)
    manage_bot.PID_FILE.write_text("{not json")

    assert manage_bot.read_pidfile() is None


def test_read_pidfile_rejects_pidless_payload(run_dir):
    run_dir.mkdir(parents=True)
    manage_bot.PID_FILE.write_text(json.dumps({"mode": "backend"}))

    assert manage_bot.read_pidfile() is None


def test_salvage_pid_reads_a_pid_out_of_a_truncated_payload():
    assert manage_bot.salvage_pid('{"pid": 4242, "mode": "back') == 4242
    assert manage_bot.salvage_pid('{"mode": "backend"') is None
    assert manage_bot.salvage_pid("") is None
    assert manage_bot.salvage_pid('{"pid": 0, "mode": "backend"}') is None


def test_unlink_pidfile_forced_ignores_ownership(run_dir):
    """Only for callers that already proved the pid it names is gone."""
    manage_bot.write_pidfile(_pid_entry(os.getpid()))

    assert manage_bot._unlink_pidfile_forced() is True
    assert not manage_bot.PID_FILE.exists()
    assert manage_bot._unlink_pidfile_forced() is False


def test_pid_alive_false_for_dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()

    assert manage_bot.pid_alive(proc.pid) is False
    assert manage_bot.pid_alive(0) is False


def test_pid_alive_true_for_live_and_own_pid():
    assert manage_bot.pid_alive(os.getpid()) is True


def test_liveness_polling_spawns_ps_at_most_once(run_dir, monkeypatch, tmp_path):
    proc, _ = _spawn_stub(tmp_path, managed=True, cooperative=False)
    calls: list[str] = []
    real_ps = manage_bot._ps
    monkeypatch.setattr(
        manage_bot, "_ps", lambda pid, field: calls.append(field) or real_ps(pid, field)
    )
    try:
        # The stub survives every signal, so only kill(0) can answer while polling;
        # the single `ps` call at the end settles "exited but not reaped".
        assert manage_bot._wait_for_exit(proc.pid, 0.25) is False
        assert calls == ["stat="]
    finally:
        _reap(proc)


def test_build_command_toggles_headless_and_forwards_paths():
    shared = {
        "bootstrap_servers": "localhost:9092",
        "config": "config.json",
        "data_dir": "./data",
        "db": "data/arena.db",
    }

    detached = manage_bot.build_command(headless=True, **shared)
    foreground = manage_bot.build_command(headless=False, **shared)
    volatile = manage_bot.build_command(headless=True, no_ledger=True, **shared)

    assert detached[:2] == [sys.executable, "-m"]
    assert detached[2] == manage_bot.MANAGED_MODULE
    assert detached[detached.index("--db") + 1] == "data/arena.db"
    assert detached[detached.index("--data-dir") + 1] == "./data"
    assert "--headless" in detached
    assert "--headless" not in foreground
    assert "--no-ledger" not in detached
    assert "--no-ledger" in volatile


# ── start ────────────────────────────────────────────────────────


def test_start_refuses_when_already_running(run_dir, capsys):
    manage_bot.write_pidfile(_pid_entry(os.getpid()))

    assert manage_bot.main(["start", "backend"]) == 1
    assert "already running" in capsys.readouterr().out


def test_start_clears_stale_pidfile_before_launching(run_dir, monkeypatch, capsys, tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    manage_bot.write_pidfile(_pid_entry(dead.pid))
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: [sys.executable, "-c", "import time; time.sleep(0.5)"],
    )

    assert manage_bot.main(["start", "backend"]) == 0

    out = capsys.readouterr().out
    assert "stale pid file" in out.lower()
    info = manage_bot.read_pidfile()
    assert info is not None and info["pid"] != dead.pid and info["mode"] == "backend"
    manage_bot.remove_pidfile(expect_pid=info["pid"])


def test_start_reports_early_exit_with_log_tail(run_dir, monkeypatch, capsys):
    def fake_build_command(**kwargs):
        return [sys.executable, "-c", "print('boom: bad config'); raise SystemExit(3)"]

    monkeypatch.setattr(manage_bot, "build_command", fake_build_command)

    assert manage_bot.main(["start", "backend"]) == 1

    out = capsys.readouterr().out
    assert "exited" in out
    assert "boom: bad config" in out
    assert not manage_bot.PID_FILE.exists()


def _raise_enospc(info: dict, path=None) -> None:
    """A full disk / unwritable .run dir, the one failure ``Popen`` cannot catch."""
    raise OSError(28, "No space left on device")


def test_start_kills_the_child_when_the_pidfile_cannot_be_written(
    run_dir, monkeypatch, capsys
):
    """S4: no pid file means nobody can ever ``stop`` the child we just launched."""
    recorded: dict = {}

    def capture_then_fail(info: dict, path=None) -> None:
        recorded["pid"] = info["pid"]
        _raise_enospc(info, path)

    monkeypatch.setattr(manage_bot, "write_pidfile", capture_then_fail)
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        assert manage_bot.main(["start", "backend"]) == 1

        out = capsys.readouterr().out
        assert "No space left on device" in out
        assert "stop" in out.lower() or "track" in out.lower()
        assert not manage_bot.PID_FILE.exists()
        assert not manage_bot.pid_alive(recorded["pid"]), "untracked engine left running"
    finally:
        if "pid" in recorded:
            try:
                os.kill(recorded["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_start_that_fails_keeps_a_pidfile_it_does_not_own(run_dir, monkeypatch):
    """A second start must never make the running engine unreachable.

    The window is real: the pid file is read, another start wins the race, our child
    dies inside the probe, and cleanup runs against a pid file that now belongs to a
    live engine. Deleting it here would leave an orphan nobody can ``stop``.
    """
    live_owner = os.getpid()  # stands in for the engine that is still running
    manage_bot.write_pidfile(_pid_entry(live_owner))
    monkeypatch.setattr(manage_bot, "read_pidfile", lambda path=None: None)  # raced past
    monkeypatch.setattr(manage_bot, "write_pidfile", lambda *a, **k: None)  # never published
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: [sys.executable, "-c", "raise SystemExit(4)"],
    )

    assert manage_bot.main(["start", "backend"]) == 1

    kept = json.loads(manage_bot.PID_FILE.read_text())
    assert kept["pid"] == live_owner, "the live engine's pid file was deleted"


def test_start_refuses_when_an_unreadable_pidfile_names_a_live_engine(run_dir, monkeypatch, capsys):
    """R4-1: publishing over an unparsable pid file strands the engine it names.

    ``start`` cannot tell "no pid file" from "pid file I failed to parse", and its
    only write path publishes unconditionally — the same orphan ``stop`` already
    guards against, through the other command.
    """
    live_owner = os.getpid()  # stands in for the engine that is still running
    _write_raw_pidfile(f'{{"pid": {live_owner}, "mode": "back')  # truncated tail
    spawned: list[dict] = []

    def fake_build_command(**kwargs) -> list[str]:
        spawned.append(kwargs)
        return ["true"]

    monkeypatch.setattr(manage_bot, "build_command", fake_build_command)

    assert manage_bot.main(["start", "backend"]) == 1

    out = capsys.readouterr().out
    assert f"live pid {live_owner}" in out
    assert "refusing to overwrite" in out
    assert spawned == [], "a second engine was spawned against a live pid file"
    assert manage_bot.PID_FILE.read_text().startswith('{"pid": ' + str(live_owner))


def test_start_replaces_an_unreadable_pidfile_once_its_pid_is_gone(run_dir, monkeypatch):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _write_raw_pidfile(f'{{"pid": {dead.pid}, "mode": "back')
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: [sys.executable, "-c", "import time; time.sleep(0.6)"],
    )

    assert manage_bot.main(["start", "backend"]) == 0

    info = manage_bot.read_pidfile()
    assert info is not None and info["pid"] != dead.pid, "stale payload survived a start"
    os.kill(info["pid"], signal.SIGKILL)
    manage_bot.remove_pidfile(expect_pid=info["pid"])


def test_start_logs_to_run_dir_and_keeps_pidfile_while_child_survives(run_dir, monkeypatch):
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: [sys.executable, "-c", "import time; time.sleep(0.6)"],
    )

    assert manage_bot.main(["start", "backend"]) == 0

    info = manage_bot.read_pidfile()
    assert info is not None and info["mode"] == "backend"
    assert manage_bot.LOG_FILE.exists()
    assert manage_bot.LOG_FILE.parent == run_dir
    # The probe passed, so the process outlived launch; clean it up for later tests.
    os.kill(info["pid"], signal.SIGKILL)
    manage_bot.remove_pidfile(expect_pid=info["pid"])


def test_start_fresh_archives_previous_wallet(run_dir, monkeypatch, tmp_path, capsys):
    db = tmp_path / "data" / "arena.db"
    db.parent.mkdir(parents=True)
    with Ledger(db):
        pass
    captured = {}

    def fake_build_command(**kwargs):
        captured.update(kwargs)
        return [sys.executable, "-c", "import time; time.sleep(0.6)"]

    monkeypatch.setattr(manage_bot, "build_command", fake_build_command)

    assert manage_bot.main(["start", "backend", "--fresh"]) == 0

    assert "Archived" in capsys.readouterr().out
    archives = list((db.parent / "archive").glob("arena-*.db"))
    assert len(archives) == 1
    assert not db.exists()  # archived away, so the child starts from $100 again
    assert captured["db"] == str(db)
    assert captured["headless"] is True
    info = manage_bot.read_pidfile()
    assert info is not None
    os.kill(info["pid"], signal.SIGKILL)
    manage_bot.remove_pidfile(expect_pid=info["pid"])


def test_start_fresh_with_no_ledger_does_not_archive_anything(
    run_dir, monkeypatch, tmp_path, capsys
):
    """A volatile engine never opens the DB, so --fresh has nothing to reset."""
    db = tmp_path / "data" / "arena.db"
    _write_ledger_fill(db)
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: [sys.executable, "-c", "import time; time.sleep(0.6)"],
    )

    assert manage_bot.main(["start", "backend", "--fresh", "--no-ledger"]) == 0

    out = capsys.readouterr().out
    assert "--fresh" in out and "--no-ledger" in out
    assert db.exists(), "the ledger must not be archived for a volatile engine"
    assert not (db.parent / "archive").exists()

    info = manage_bot.read_pidfile()
    assert info is not None
    os.kill(info["pid"], signal.SIGKILL)
    manage_bot.remove_pidfile(expect_pid=info["pid"])


def test_start_tui_runs_foreground_and_cleans_pidfile(run_dir, monkeypatch):
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: [sys.executable, "-c", f"# {manage_bot.MANAGED_MODULE}\nraise SystemExit(7)"],
    )

    assert manage_bot.main(["start", "tui"]) == 7
    assert not manage_bot.PID_FILE.exists()


def test_start_tui_stops_the_child_before_dropping_its_pidfile(run_dir, tmp_path, monkeypatch):
    proc, marker = _spawn_stub(tmp_path, managed=True, cooperative=True)
    try:
        _patch_tui_spawn(monkeypatch, proc)

        assert manage_bot.main(["start", "tui"]) == 130

        assert int(marker.read_text()) == signal.SIGINT
        assert proc.wait(timeout=5) == 0
        assert not manage_bot.PID_FILE.exists()
    finally:
        _reap(proc)


def test_start_tui_keeps_pidfile_when_the_child_refuses_to_exit(
    run_dir, tmp_path, monkeypatch, capsys
):
    proc, _ = _spawn_stub(tmp_path, managed=True, cooperative=False)
    try:
        _patch_tui_spawn(monkeypatch, proc)

        assert manage_bot.main(["start", "tui"]) == 2

        out = capsys.readouterr().out
        assert "SIGTERM" in out
        assert "stop --force" in out
        info = manage_bot.read_pidfile()
        assert info is not None and info["pid"] == proc.pid, (
            "a child that ignored SIGINT/SIGTERM is still trading; its pid file must survive"
        )
        assert proc.poll() is None
    finally:
        _reap(proc)  # SIGKILL + wait: never leave a stub child behind


def test_start_tui_stops_the_child_when_the_pidfile_cannot_be_written(
    run_dir, tmp_path, monkeypatch, capsys
):
    """S4 for the foreground path: the same ENOSPC window, no operator "Ctrl-C" here."""
    proc, _ = _spawn_stub(tmp_path, managed=True, cooperative=True)
    real_popen = subprocess.Popen
    monkeypatch.setattr(manage_bot, "build_command", lambda **kwargs: ["stub-tui"])
    monkeypatch.setattr(
        manage_bot.subprocess,
        "Popen",
        lambda *args, **kwargs: (
            proc if args and args[0] == ["stub-tui"] else real_popen(*args, **kwargs)
        ),
    )
    monkeypatch.setattr(manage_bot, "write_pidfile", _raise_enospc)
    try:
        assert manage_bot.main(["start", "tui"]) == 1

        out = capsys.readouterr().out
        assert "No space left on device" in out
        assert not manage_bot.PID_FILE.exists()
        assert proc.wait(timeout=5) == 0, "the TUI child outlived a failed pid-file write"
    finally:
        _reap(proc)


# ── stop ─────────────────────────────────────────────────────────


def test_stop_sends_sigint_and_removes_pidfile(run_dir, tmp_path):
    proc, marker = _spawn_stub(tmp_path, managed=True)
    try:
        manage_bot.write_pidfile(_pid_entry(proc.pid))

        assert manage_bot.main(["stop"]) == 0

        assert marker.exists(), "child never received a signal"
        assert int(marker.read_text()) == signal.SIGINT
        assert not manage_bot.PID_FILE.exists()
        assert proc.wait(timeout=5) == 0
    finally:
        _reap(proc)


def test_stop_without_pidfile_reports_not_running(run_dir, capsys):
    assert manage_bot.main(["stop"]) == 1
    assert "not running" in capsys.readouterr().out


def test_stop_cleans_stale_pidfile(run_dir, capsys):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    manage_bot.write_pidfile(_pid_entry(dead.pid))

    assert manage_bot.main(["stop"]) == 1
    assert "stale pid file" in capsys.readouterr().out.lower()
    assert not manage_bot.PID_FILE.exists()


def _write_raw_pidfile(contents: str) -> None:
    """Publish bytes that ``read_pidfile`` rejects but a human can still debug."""
    manage_bot.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    manage_bot.PID_FILE.write_text(contents)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits")
def test_stop_keeps_a_pidfile_it_cannot_read(run_dir, capsys):
    """M1: an unreadable pid file is not proof that no engine is running.

    Deleting it here is how a live engine becomes unreachable: ``stop`` would then
    report "no pid file" forever while the orphan keeps trading.
    """
    manage_bot.write_pidfile(_pid_entry(os.getpid()))
    manage_bot.PID_FILE.chmod(0o000)
    try:
        assert manage_bot.main(["stop"]) == 1
        assert manage_bot.PID_FILE.exists(), "a live owner lost its pid file"
    finally:
        manage_bot.PID_FILE.chmod(0o600)

    out = capsys.readouterr().out
    assert f"unreadable pid file {manage_bot.PID_FILE}" in out
    assert "remove it manually if no engine is running" in out
    assert json.loads(manage_bot.PID_FILE.read_text())["pid"] == os.getpid()


def test_stop_removes_a_corrupt_pidfile_only_once_its_pid_is_gone(run_dir, capsys):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _write_raw_pidfile(f'{{"pid": {dead.pid}, "mode": "back')  # truncated tail

    assert manage_bot.main(["stop"]) == 1

    assert not manage_bot.PID_FILE.exists()
    assert f"pid {dead.pid} is gone" in capsys.readouterr().out


def test_stop_keeps_a_corrupt_pidfile_whose_salvaged_pid_is_alive(run_dir, capsys):
    _write_raw_pidfile(f'{{"pid": {os.getpid()}, "mode": "back')  # owner is this process

    assert manage_bot.main(["stop"]) == 1

    assert manage_bot.PID_FILE.exists(), "the salvaged owner is alive; fail closed"
    assert "remove it manually if no engine is running" in capsys.readouterr().out


def test_stop_fails_closed_on_a_pidfile_without_a_salvageable_pid(run_dir, capsys):
    _write_raw_pidfile('{"mode": "backend"')  # truncated JSON, no pid anywhere

    assert manage_bot.main(["stop"]) == 1

    assert manage_bot.PID_FILE.exists(), "unparsable and unowned: leave it to the operator"
    assert "remove it manually if no engine is running" in capsys.readouterr().out


def test_stop_treats_a_transient_read_failure_as_unknown_ownership(
    run_dir, monkeypatch, capsys
):
    """The internal race: read_pidfile returns None while the file itself is fine."""
    live_owner = os.getpid()
    manage_bot.write_pidfile(_pid_entry(live_owner))
    monkeypatch.setattr(manage_bot, "read_pidfile", lambda path=None: None)

    assert manage_bot.main(["stop"]) == 1

    assert manage_bot.PID_FILE.exists()
    assert json.loads(manage_bot.PID_FILE.read_text())["pid"] == live_owner


def test_stop_never_signals_unverified_pid(run_dir, tmp_path, capsys):
    proc, marker = _spawn_stub(tmp_path, managed=False)
    try:
        manage_bot.write_pidfile(_pid_entry(proc.pid))

        assert manage_bot.main(["stop"]) == 1

        assert "Refusing" in capsys.readouterr().out
        assert not manage_bot.PID_FILE.exists()
        assert proc.poll() is None, "stop must not kill a pid it cannot verify"
        assert not marker.exists()
    finally:
        _reap(proc)


def test_stop_escalates_and_requires_force_for_sigkill(run_dir, tmp_path, capsys):
    proc, marker = _spawn_stub(tmp_path, managed=True, cooperative=False)
    try:
        manage_bot.write_pidfile(_pid_entry(proc.pid))

        assert manage_bot.main(["stop"]) == 2
        assert "SIGKILL" in capsys.readouterr().out
        assert proc.poll() is None
        assert manage_bot.PID_FILE.exists()

        assert manage_bot.main(["stop", "--force"]) == 0
        assert not manage_bot.PID_FILE.exists()
        assert proc.wait(timeout=5) == -signal.SIGKILL
        assert not marker.exists()
    finally:
        _reap(proc)


def test_stop_does_not_escalate_after_the_pid_stops_looking_like_the_bot(
    run_dir, monkeypatch, capsys
):
    """A pid recycled during the grace window is not the bot — never SIGTERM it."""
    pid = os.getpid()  # alive, so the ladder starts; every signal below is stubbed out
    manage_bot.write_pidfile(_pid_entry(pid))
    checks = {"n": 0}

    def flaky_owner(candidate: int) -> bool:
        checks["n"] += 1
        return checks["n"] == 1  # verified once by cmd_stop; recycled by the next check

    sent: list[int] = []
    monkeypatch.setattr(manage_bot, "is_managed_process", flaky_owner)
    monkeypatch.setattr(
        manage_bot, "_signal", lambda candidate, sig: sent.append(sig) or True
    )

    assert manage_bot.main(["stop"]) == 1

    assert sent == [signal.SIGINT], f"escalated to {sent} without re-verifying the owner"
    out = capsys.readouterr().out
    assert "no longer" in out


# ── status ───────────────────────────────────────────────────────


def test_status_when_stopped(run_dir, capsys):
    assert manage_bot.main(["status"]) == 0

    out = capsys.readouterr().out
    assert "not running" in out
    assert "no wallet state yet" in out


def test_status_reports_stale_pidfile(run_dir, capsys):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    manage_bot.write_pidfile(_pid_entry(dead.pid))

    status = manage_bot.build_status()

    assert status["process"]["state"] == "stale"
    assert "stale" in manage_bot.render_status(status).lower()


def test_status_when_running_reports_pid_mode_and_uptime(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(manage_bot, "is_managed_process", lambda pid: True)
    info = _pid_entry(os.getpid())
    info["started_at"] = (datetime.now() - timedelta(seconds=90)).isoformat()
    manage_bot.write_pidfile(info)

    status = manage_bot.build_status()

    proc = status["process"]
    assert proc["state"] == "running"
    assert proc["verified"] is True
    assert proc["pid"] == os.getpid()
    assert proc["mode"] == "backend"
    assert 90 <= proc["uptime_seconds"] < 120
    text = manage_bot.render_status(status)
    assert "running" in text
    assert str(os.getpid()) in text
    assert "backend" in text


def test_status_marks_unverified_process(run_dir):
    manage_bot.write_pidfile(_pid_entry(os.getpid()))

    status = manage_bot.build_status()

    assert status["process"]["state"] == "running"
    assert status["process"]["verified"] is False
    assert "unverified" in manage_bot.render_status(status).lower()


def _write_ledger_fill(db, agent_id: str = "agent-1") -> None:
    with Ledger(db) as ledger:
        account = AgentAccount(cash=99_000.0, trade_count=1, total_fees_paid=10.0)
        account.positions["BTC-USD"] = 0.02
        account.cost_basis["BTC-USD"] = 1_000.0
        ledger.save_fill(
            agent_id=agent_id,
            account=account,
            timestamp=datetime.now().isoformat(),
            action="buy",
            product_id="BTC-USD",
            quantity=0.02,
            price=50_000.0,
            fee=10.0,
            cash_after=99_000.0,
            latency=0.5,
        )


def test_status_reads_wallet_from_ledger(run_dir, tmp_path, capsys):
    db = tmp_path / "data" / "arena.db"
    _write_ledger_fill(db)

    assert manage_bot.main(["status", "--db", str(db), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["process"]["state"] == "stopped"
    assert payload["wallet"]["db"] == str(db)
    assert payload["wallet"]["error"] is None
    row = payload["wallet"]["agents"][0]
    assert row["agent_id"] == "agent-1"
    assert row["cash"] == 99_000.0
    assert row["position_count"] == 1
    assert row["trade_count"] == 1
    assert row["last_fill_ts"] is not None


def test_status_renders_wallet_table(run_dir, tmp_path, capsys):
    db = tmp_path / "data" / "arena.db"
    _write_ledger_fill(db, agent_id="momentum-trader")

    assert manage_bot.main(["status", "--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "momentum-trader" in out
    assert "99,000.00" in out


def test_status_reports_a_degraded_persistence_marker(run_dir, tmp_path, capsys):
    """The engine cannot log to a TUI operator after Live(screen=True) clears it."""
    db = tmp_path / "data" / "arena.db"
    _write_ledger_fill(db)
    with Ledger(db) as ledger:
        ledger.mark_degraded("momentum", "2025-01-01T00:00:00")

    assert manage_bot.main(["status", "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wallet"]["degraded"] == {
        "ts": "2025-01-01T00:00:00",
        "agent": "momentum",
    }

    assert manage_bot.main(["status", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "persistence degraded at 2025-01-01 00:00:00 (momentum)" in out
    assert "fills after that point may diverge from disk" in out


def test_status_is_quiet_without_a_degraded_marker(run_dir, tmp_path, capsys):
    db = tmp_path / "data" / "arena.db"
    _write_ledger_fill(db)

    assert manage_bot.main(["status", "--db", str(db)]) == 0

    assert "WARNING" not in capsys.readouterr().out


def test_status_tolerates_foreign_database_file(run_dir, tmp_path, capsys):
    db = tmp_path / "data" / "arena.db"
    db.parent.mkdir(parents=True)
    db.write_text("not a sqlite database")

    assert manage_bot.main(["status", "--db", str(db), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["wallet"]["error"], "unreadable ledger should be reported, not raised"
    assert payload["wallet"]["agents"] == []


def test_resolve_db_prefers_flag_then_pidfile_then_default(run_dir, tmp_path):
    recorded = tmp_path / "data" / "recorded.db"
    spaced = {"cmd": [sys.executable, "-m", manage_bot.MANAGED_MODULE, "--db", str(recorded)]}
    joined = {"cmd": [sys.executable, f"--db={recorded}"]}

    assert manage_bot.resolve_db(str(recorded), {"cmd": ["unrelated"]}) == recorded
    assert manage_bot.resolve_db(None, spaced) == recorded
    assert manage_bot.resolve_db(None, joined) == recorded
    assert manage_bot.resolve_db(None, None) == manage_bot.DEFAULT_DB
    assert manage_bot.resolve_db(None, {"cmd": ["--db"]}) == manage_bot.DEFAULT_DB


def test_status_uses_the_db_recorded_in_the_pidfile(run_dir, tmp_path, capsys):
    recorded = tmp_path / "data" / "recorded.db"
    explicit = tmp_path / "data" / "explicit.db"
    _write_ledger_fill(recorded, agent_id="recorded-agent")
    _write_ledger_fill(explicit, agent_id="explicit-agent")
    info = _pid_entry(os.getpid())
    info["cmd"] = [sys.executable, "-m", manage_bot.MANAGED_MODULE, "--db", str(recorded)]
    manage_bot.write_pidfile(info)

    assert manage_bot.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wallet"]["db"] == str(recorded)
    assert payload["wallet"]["agents"][0]["agent_id"] == "recorded-agent"

    assert manage_bot.main(["status", "--db", str(explicit), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wallet"]["db"] == str(explicit)
    assert payload["wallet"]["agents"][0]["agent_id"] == "explicit-agent"


def test_status_reads_a_ledger_the_engine_is_still_writing(run_dir, tmp_path, capsys):
    db = tmp_path / "data" / "arena.db"
    with Ledger(db) as ledger:  # a live writer, exactly like a running engine
        account = AgentAccount(cash=49_000.0, trade_count=3, total_fees_paid=30.0)
        account.positions["ETH-USD"] = 1.5
        account.cost_basis["ETH-USD"] = 2_000.0
        ledger.save_fill(
            agent_id="agent-1",
            account=account,
            timestamp=datetime.now().isoformat(),
            action="buy",
            product_id="ETH-USD",
            quantity=1.5,
            price=2_000.0,
            fee=30.0,
            cash_after=49_000.0,
            latency=0.5,
        )

        # status must not need the single-writer lock to report a running engine.
        assert manage_bot.main(["status", "--db", str(db), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["wallet"]["error"] is None
    row = payload["wallet"]["agents"][0]
    assert row["trade_count"] == 3
    assert row["position_count"] == 1
    assert row["last_fill_ts"] is not None


# ── pid file durability ─────────────────────────────────────────


def test_write_pidfile_is_atomic_and_leaves_no_temp_files(run_dir):
    manage_bot.write_pidfile(_pid_entry(1111))
    manage_bot.write_pidfile(_pid_entry(2222, "tui"))

    assert [path.name for path in run_dir.iterdir()] == [manage_bot.PID_FILE.name]
    info = manage_bot.read_pidfile()
    assert info is not None and info["pid"] == 2222 and info["mode"] == "tui"


# ── --fresh archiving and the ledger exclusivity pre-check ──────


def test_archive_refuses_while_an_engine_holds_the_ledger(run_dir, tmp_path):
    db = tmp_path / "data" / "arena.db"

    with Ledger(db):
        with pytest.raises(LedgerInUseError):
            manage_bot.archive_ledger(db)

        assert db.exists(), "refusing must not move anything"
        assert not (db.parent / "archive").exists()


def test_start_fresh_refuses_while_an_engine_holds_the_ledger(
    run_dir, tmp_path, monkeypatch, capsys
):
    db = tmp_path / "data" / "arena.db"
    spawned: list[dict] = []
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: spawned.append(kwargs) or [sys.executable, "-c", "pass"],
    )

    with Ledger(db):
        assert manage_bot.main(["start", "backend", "--fresh", "--db", str(db)]) == 1

    out = capsys.readouterr().out
    assert "refusing" in out.lower()
    assert str(db) in out
    assert spawned == [], "must not launch an engine while another owns the ledger"
    assert db.exists()
    assert list((db.parent / "archive").glob("*")) == []
    assert not manage_bot.PID_FILE.exists()


def test_start_fresh_reports_a_filesystem_error_as_a_cli_error(
    run_dir, tmp_path, monkeypatch, capsys
):
    """An unmapped lock/open OSError must not surface as a traceback for the operator."""
    db = tmp_path / "data" / "arena.db"
    _write_ledger_fill(db)
    spawned: list[dict] = []

    def no_read_only_dir(path):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(manage_bot, "_archive_dir", no_read_only_dir)
    monkeypatch.setattr(
        manage_bot, "build_command", lambda **kwargs: spawned.append(kwargs) or ["unused"]
    )

    assert manage_bot.main(["start", "backend", "--fresh", "--db", str(db)]) == 1

    out = capsys.readouterr().out
    assert "cannot archive" in out
    assert "Read-only file system" in out
    assert spawned == []
    assert db.exists()


def test_start_refuses_when_another_engine_owns_the_ledger(
    run_dir, tmp_path, monkeypatch, capsys
):
    db = tmp_path / "data" / "arena.db"
    spawned: list[dict] = []
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: spawned.append(kwargs) or [sys.executable, "-c", "pass"],
    )

    with Ledger(db):
        assert manage_bot.main(["start", "backend", "--db", str(db)]) == 1

    out = capsys.readouterr().out
    assert "already" in out.lower()
    assert spawned == []
    assert not manage_bot.PID_FILE.exists()


def test_start_skips_the_ledger_precheck_with_no_ledger(run_dir, tmp_path, monkeypatch):
    db = tmp_path / "data" / "arena.db"
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: [sys.executable, "-c", "import time; time.sleep(0.6)"],
    )

    with Ledger(db):
        assert manage_bot.main(["start", "backend", "--db", str(db), "--no-ledger"]) == 0

    info = manage_bot.read_pidfile()
    assert info is not None
    os.kill(info["pid"], signal.SIGKILL)
    manage_bot.remove_pidfile(expect_pid=info["pid"])


def test_archive_moves_sidecars_and_keeps_archive_names_unique(run_dir, tmp_path):
    db = tmp_path / "data" / "arena.db"
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    subprocess.run([sys.executable, "-c", CRASH_WRITER, str(db)], env=env, check=False)
    for suffix in ("-wal", "-shm", ".lock"):
        assert Path(f"{db}{suffix}").exists(), f"the crash writer should leave {suffix}"

    first = manage_bot.archive_ledger(db)

    assert first is not None and first.exists()
    assert len(first.stem.split("-")) == 4, "archive names need microsecond resolution"
    assert not db.exists()
    for suffix in ("-wal", "-shm", ".lock"):
        assert not Path(f"{db}{suffix}").exists(), f"{suffix} left behind"
        assert Path(f"{first}{suffix}").exists(), f"{suffix} must travel with the database"
    with Ledger(first, read_only=True) as archived:
        assert [row["agent_id"] for row in archived.summary()] == ["crash-agent"]

    _write_ledger_fill(db, agent_id="agent-2")
    second = manage_bot.archive_ledger(db)

    assert second is not None and second != first and second.exists()
    assert len(list((db.parent / "archive").glob("arena-*.db"))) == 2


def test_archive_discards_orphan_sidecars_when_the_database_is_gone(run_dir, tmp_path):
    db = tmp_path / "data" / "arena.db"
    db.parent.mkdir(parents=True)
    for suffix in ("-wal", "-shm", ".lock"):
        Path(f"{db}{suffix}").write_text("")

    assert manage_bot.archive_ledger(db) is None

    for suffix in ("-wal", "-shm"):
        assert not Path(f"{db}{suffix}").exists(), f"orphan {suffix} left behind"
    # The lock file is never unlinked: a writer may already hold that inode and be a
    # moment away from creating the database, and a fresh file would let a second one in.
    assert Path(f"{db}.lock").exists(), "the orphan sweep must never unlink the lock"


def test_archive_never_deletes_a_lock_a_writer_still_holds(run_dir, tmp_path):
    db = tmp_path / "data" / "arena.db"
    db.parent.mkdir(parents=True)
    lock_path = Path(f"{db}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # No database file yet, but a writer owns the lock: deleting it would let a
        # second engine in, so the orphan sweep has to refuse instead.
        with pytest.raises(LedgerInUseError):
            manage_bot.archive_ledger(db)

        assert lock_path.exists()
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


# ── --no-ledger passthrough ─────────────────────────────────────


def test_start_forwards_no_ledger_and_status_warns(run_dir, monkeypatch, capsys):
    captured: dict = {}
    monkeypatch.setattr(
        manage_bot,
        "build_command",
        lambda **kwargs: captured.update(kwargs)
        or [sys.executable, "-c", "import time; time.sleep(0.6)"],
    )

    assert manage_bot.main(["start", "backend", "--no-ledger"]) == 0

    assert captured["no_ledger"] is True
    info = manage_bot.read_pidfile()
    assert info is not None and info["no_ledger"] is True

    capsys.readouterr()  # drop the start output
    assert manage_bot.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "persistence disabled (started with --no-ledger)" in out

    os.kill(info["pid"], signal.SIGKILL)
    manage_bot.remove_pidfile(expect_pid=info["pid"])


# ── Live trading controls (halt / resume / reconcile / live-init) ──


class _VenueBalance:
    def __init__(self, free: float) -> None:
        self.free = free
        self.total = free


class _VenueClient:
    """Stands in for BinanceSpotClient: balances and open orders only."""

    def __init__(self, balances: dict[str, float], *, open_orders: int = 0) -> None:
        self._balances = balances
        self._open = list(range(open_orders))
        self.closed = False

    def balances(self):
        return {asset: _VenueBalance(amount) for asset, amount in self._balances.items()}

    def open_orders(self):
        return list(self._open)

    def close(self):
        self.closed = True


def _live_trading_config(*, reserve: float = 5.0, agents=("a1",)):
    from types import SimpleNamespace

    return SimpleNamespace(
        trading=SimpleNamespace(
            mode="live",
            live=SimpleNamespace(
                agent_accounts={agent: "main" for agent in agents},
                allow_symbols=["SOLUSDT"],
                reserve_usdt=reserve,
                recv_window_ms=5000,
            ),
        )
    )


EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "SOLUSDT",
            "status": "TRADING",
            "baseAsset": "SOL",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
            ],
        }
    ]
}


@pytest.fixture
def live_venue(monkeypatch, run_dir, tmp_path):
    """Point the live commands at fake venue credentials and a fake account."""

    def _install(
        balances: dict[str, float], *, open_orders: int = 0, agents=("a1",), reserve: float = 5.0
    ):
        flag = tmp_path / "data" / "LIVE_HALT"
        client = _VenueClient(balances, open_orders=open_orders)
        cfg = _live_trading_config(reserve=reserve, agents=agents)
        monkeypatch.setattr(manage_bot, "_live_config_or_exit", lambda path: cfg)
        monkeypatch.setattr(
            manage_bot, "_venue_client_or_exit", lambda _cfg: (client, "main", EXCHANGE_INFO)
        )
        return SimpleNamespace(flag=flag, client=client, cfg=cfg)

    return _install


from types import SimpleNamespace  # noqa: E402  (used by the fixture above)


def test_halt_writes_a_flag_with_the_reason(live_venue, run_dir, tmp_path):
    venue = live_venue({"USDT": 100.0})
    code = manage_bot.main(
        ["halt", "--reason", "watching a whale", "--flag", str(venue.flag)]
    )

    assert code == 0
    assert venue.flag.read_text().strip() == "watching a whale"


def test_resume_refuses_while_the_bot_is_running(live_venue, monkeypatch, tmp_path):
    venue = live_venue({"USDT": 100.0})
    venue.flag.parent.mkdir(parents=True, exist_ok=True)
    venue.flag.write_text("operator\n")
    monkeypatch.setattr(manage_bot, "process_state", lambda **_: {"state": "running", "pid": 42})

    code = manage_bot.main(["resume", "--flag", str(venue.flag)])

    assert code == 1
    assert venue.flag.exists()  # a halt is never cleared behind a running engine's back


def test_resume_clears_the_flag_and_asks_the_engine_to_reconcile(live_venue, monkeypatch):
    venue = live_venue({"USDT": 100.0})
    venue.flag.parent.mkdir(parents=True, exist_ok=True)
    venue.flag.write_text("operator\n")
    monkeypatch.setattr(manage_bot, "process_state", lambda **_: {"state": "stopped", "pid": None})

    code = manage_bot.main(["resume", "--flag", str(venue.flag)])

    assert code == 0
    assert not venue.flag.exists()
    with Ledger(manage_bot.DEFAULT_DB, read_only=True) as ledger:
        assert ledger.get_meta("live_state") == "RECONCILING"


def test_reconcile_reports_a_clean_wallet(live_venue, capsys):
    nonlocal_venue = live_venue({"USDT": 95.0, "SOL": 0.0}, reserve=0.0)
    with Ledger(manage_bot.DEFAULT_DB) as ledger:
        ledger.set_meta("live_baseline", json.dumps({"USDT": 0.0, "SOL": 0.0}))
        ledger.persist_account("a1", AgentAccount(cash=95.0))

    code = manage_bot.main(["reconcile"])

    assert code == 0
    assert "reconciled" in capsys.readouterr().out
    assert nonlocal_venue.client.closed


def test_reconcile_fails_on_a_shortfall(live_venue, capsys):
    live_venue({"USDT": 95.0, "SOL": 0.0}, reserve=0.0)
    with Ledger(manage_bot.DEFAULT_DB) as ledger:
        ledger.set_meta("live_baseline", json.dumps({"USDT": 0.0, "SOL": 0.0}))
        account = AgentAccount(cash=95.0)
        account.positions["SOLUSDT"] = 0.5
        account.cost_basis["SOLUSDT"] = 50.0
        ledger.persist_account("a1", account)

    code = manage_bot.main(["reconcile"])

    assert code == 1
    out = capsys.readouterr().out
    assert "NOT reconciled" in out and "short 0.5" in out
    assert "--write-down" in out


def test_reconcile_json_is_machine_readable(live_venue, capsys):
    live_venue({"USDT": 95.0, "SOL": 0.0}, reserve=0.0)
    with Ledger(manage_bot.DEFAULT_DB) as ledger:
        ledger.set_meta("live_baseline", json.dumps({"USDT": 0.0, "SOL": 0.0}))
        ledger.persist_account("a1", AgentAccount(cash=95.0))

    assert manage_bot.main(["reconcile", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["armed"] is True
    assert payload["ledger_cash"] == pytest.approx(95.0)


def test_live_init_records_the_baseline_and_seeds_the_agent(live_venue, monkeypatch, capsys):
    live_venue({"USDT": 100.0, "SOL": 0.0})
    monkeypatch.setattr(manage_bot, "process_state", lambda **_: {"state": "stopped", "pid": None})

    code = manage_bot.main(["live-init", "--fund", "95"])

    assert code == 0
    with Ledger(manage_bot.DEFAULT_DB, read_only=True) as ledger:
        baseline = json.loads(ledger.get_meta("live_baseline"))
        agent = ledger.load_accounts()["a1"]
        events = ledger.equity_events()
    # 100 free − 95 allocated − 5 reserve = 0 left unallocated
    assert baseline == {"USDT": pytest.approx(0.0), "SOL": pytest.approx(0.0)}
    assert agent.cash == pytest.approx(95.0)
    assert agent.positions == {}
    assert events[0]["kind"] == "initial_allocation"
    assert "reconcile" in capsys.readouterr().out


def test_a_pre_existing_base_holding_is_baselined_not_allocated(live_venue, monkeypatch):
    live_venue({"USDT": 100.0, "SOL": 1.5})
    monkeypatch.setattr(manage_bot, "process_state", lambda **_: {"state": "stopped", "pid": None})

    assert manage_bot.main(["live-init", "--fund", "95"]) == 0

    with Ledger(manage_bot.DEFAULT_DB, read_only=True) as ledger:
        baseline = json.loads(ledger.get_meta("live_baseline"))
    assert baseline["SOL"] == pytest.approx(1.5)


def test_live_init_then_reconcile_arms(live_venue, monkeypatch, capsys):
    """The baseline arithmetic must make the very next reconcile clean."""
    live_venue({"USDT": 100.0, "SOL": 0.0})
    monkeypatch.setattr(manage_bot, "process_state", lambda **_: {"state": "stopped", "pid": None})

    assert manage_bot.main(["live-init", "--fund", "95"]) == 0
    capsys.readouterr()

    assert manage_bot.main(["reconcile", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["armed"] is True


def test_live_init_refuses_more_than_the_wallet_holds(live_venue, monkeypatch, capsys):
    live_venue({"USDT": 50.0, "SOL": 0.0})
    monkeypatch.setattr(manage_bot, "process_state", lambda **_: {"state": "stopped", "pid": None})

    code = manage_bot.main(["live-init", "--fund", "95"])

    assert code == 1
    assert "needs 100.00" in capsys.readouterr().out


def test_live_init_refuses_to_overwrite_live_history(live_venue, monkeypatch, capsys):
    live_venue({"USDT": 200.0, "SOL": 0.0})
    monkeypatch.setattr(manage_bot, "process_state", lambda **_: {"state": "stopped", "pid": None})
    with Ledger(manage_bot.DEFAULT_DB) as ledger:
        ledger.persist_account("a1", AgentAccount(cash=10.0, trade_count=3))

    assert manage_bot.main(["live-init", "--fund", "95"]) == 1
    assert "already has live history" in capsys.readouterr().out

    assert manage_bot.main(["live-init", "--fund", "95", "--force"]) == 0


def test_live_init_needs_the_engine_stopped(live_venue, monkeypatch, capsys):
    live_venue({"USDT": 100.0})
    monkeypatch.setattr(manage_bot, "process_state", lambda **_: {"state": "running", "pid": 7})

    assert manage_bot.main(["live-init", "--fund", "95"]) == 1
    assert "needs the wallet ledger to itself" in capsys.readouterr().out


def _write_live_config(tmp_path) -> str:
    """A minimal *valid* live-mode config, so `status` reports mode=live."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "agents": [{"name": "a1", "provider": "p", "model": "m"}],
                "llm_providers": {"p": {"api_key": "k"}},
                "trading": {
                    "mode": "live",
                    "live": {
                        "accounts": {"main": {"api_key": "k", "api_secret": "s"}},
                        "agent_accounts": {"a1": "main"},
                        "allow_symbols": ["SOLUSDT"],
                    },
                },
            }
        )
    )
    return str(path)


def test_status_shows_the_live_state(live_venue, tmp_path, capsys):
    live_venue({"USDT": 95.0})
    config = _write_live_config(tmp_path)
    with Ledger(manage_bot.DEFAULT_DB) as ledger:
        ledger.set_meta("live_state", "HALTED")
        ledger.set_meta("live_halt_reason", "SOL short 0.5")

    manage_bot.main(["status", "--config", config])

    out = capsys.readouterr().out
    assert "Live:    HALTED — SOL short 0.5" in out


def test_status_prefers_the_halt_flag_over_the_recorded_state(live_venue, tmp_path, capsys):
    venue = live_venue({"USDT": 95.0})
    config = _write_live_config(tmp_path)
    venue.flag.parent.mkdir(parents=True, exist_ok=True)
    venue.flag.write_text("operator\n")
    with Ledger(manage_bot.DEFAULT_DB) as ledger:
        ledger.set_meta("live_state", "ARMED")

    manage_bot.main(["status", "--config", config])

    assert "LIVE_HALT is set" in capsys.readouterr().out


def test_status_reports_paper_mode_instead_of_dormant_live_state(live_venue, tmp_path, capsys):
    live_venue({"USDT": 95.0})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"trading": {"mode": "paper"}}))
    with Ledger(manage_bot.DEFAULT_DB) as ledger:
        ledger.set_meta("live_state", "RECONCILING")

    manage_bot.main(["status", "--config", str(config)])

    assert "Live:    not in use (trading.mode = paper)" in capsys.readouterr().out
