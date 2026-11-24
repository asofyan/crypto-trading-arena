# Persist wallet state to a SQLite ledger

Arena agent wallets lived only in memory, so every restart reset all agents to
`$100k`. We now persist them to SQLite (`data/arena.db`, tables `accounts`,
`positions`, `fills`, `meta`) so a restart resumes the session instead of
starting over — accepting specific durability and failure-mode trade-offs to
keep disk work off the trading hot path.

## Considered Options

- **Single-writer locking.** Exactly one writer per database via an advisory
  flock on `<db>.lock`; readers (`manage_bot.py status`) open with
  `read_only=True`, which takes no lock. Chosen over *trusting the pid file
  alone* (a stale pid file cannot fence a second engine out of the same DB)
  and over *no lock, last-writer-wins* (two engines silently interleaving
  account writes corrupts state no reader can detect).
- **Durability level.** Each fill commits in one transaction (account +
  positions + fill together) under `journal_mode=WAL` +
  `synchronous=NORMAL`. A process crash is safe (WAL replayed on next open);
  an OS/machine crash can lose the last fill(s) still in the WAL. Accepted
  because fsync-per-fill would put disk latency on the hot path.
- **Failure mode: fail-open, not fail-closed.** A ledger write failure does
  not fail the trade — in-memory state keeps running and the operator is
  signalled (red `LEDGER DEGRADED` dashboard badge, `WARNING: persistence
  degraded …` in `manage_bot.py status`, `wallet.degraded` in `status --json`,
  plus a durable marker row in `meta` written once, when the first fill fails,
  and cleared on the next successful startup — the worker's `after_startup`
  hook with the broker up, never at attach time, so a start that dies before
  serving keeps the record). Fail-closed (reject the trade) was rejected: an
  unwritable database would halt the whole arena rather than degrade it.
  Caveat: the marker travels through the ledger's own connection, so a failure
  that leaves the database unwritable (read-only mount, `ENOSPC`, corruption)
  also blocks the marker — the in-process badge and the log remain, the durable
  row and the cross-process `status` warning do not.
- **Snapshots vs ledger.** The legacy CSV snapshots remain for
  `--snapshot-interval` history but are no longer the source of truth — the
  ledger is what hydrates `AccountStore` at startup.
- **`--fresh` archives, never deletes.** The DB and its sidecars move to
  `data/archive/`, and the command refuses while another writer holds the
  lock, so a reset can never race a live engine into a half-moved database.
