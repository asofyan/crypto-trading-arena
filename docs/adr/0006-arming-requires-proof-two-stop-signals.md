# Arming requires proof: two independent stop signals

**Status:** accepted

## Context & decision

Live trading must survive being killed at any instant, and it must never trade on a state
it has not verified. Those two requirements pull in opposite directions — automatic
resume for the first, refusing to guess for the second — so they are separated into two
signals that answer different questions.

**Signal 1 — the halt flag (`data/LIVE_HALT`).** "Do not trade until a human says so."
Written by `manage_bot.py halt`, and automatically on a hard venue failure (IP ban, an
order the venue refused outright at the link level). It is a *file*, not a database row,
because the engine holds the ledger's exclusive writer lock: a second process cannot
write the DB but can create a file, and `Path.exists()` costs nothing on the order path.
It survives restarts.

**Signal 2 — `live_state != ARMED`.** "This process could not prove it is safe."
Reconciliation runs at startup, *after* resolving every dangling order. A shortfall, an
open order the bot did not place, or a missing baseline keeps the session disarmed. On
the next start, reconciliation runs again: a drift that was transient (a partial balance
read) clears itself, so a restart genuinely resumes trading. That is the operator's
"stop anytime, restart, carry on" requirement, and it requires no exit flag.

Reconciliation never *writes* the halt flag for drift. A drift is a fact to be shown, not
a decision; only a human (or a hard venue failure) makes the stop permanent.

**Deposits and withdrawals are not symmetric.** A surplus is an event, not a
disagreement: `EquitySync` adopts a quote surplus that has held steady across two samples
and journals it to `equity_events`, which is how a hand-made top-up raises the
percentage caps with no restart and no config edit. A shortfall looks exactly like a bug
that invented money, so it blocks and requires an explicit operator write-down.

## Consequences

- Arming is automatic when the numbers agree and the flag is absent. There is no `--arm`
  step: a clean restart trades, which is what a restart is *for*.
- An operator halt cannot be undone by a restart. `resume` (engine stopped) clears the
  flag and sets `live_state = RECONCILING`, so the next start proves itself again.
- A shortfall caught mid-session disarms but does not raise the flag; if the operator
  fixes the wallet, `resume` or a restart re-arms.
- Re-arming is never silent: a cleared drift logs "run `manage_bot.py resume` to re-arm"
  rather than resuming by itself, so a transient mismatch cannot hide behind an
  automatic transition.
- `manage_bot.py status` shows both signals, and prefers the flag over the recorded state
  — a halt written while the engine was running could not touch the ledger, so the file
  is the newer truth.
