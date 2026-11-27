> **Band updated 2026-11-26.** The floor is now **100/50 bps** with a 60-minute hold
> limit (`arena/trade_policy.ExitFloor`, `config.py ExitFloorConfig`), and the
> scorecard's defaults follow it (`arena/experiment.DEFAULT_TARGET_BPS/STOP_BPS`).
> Numbers quoted below as "+50 bps" / "46.7% break-even" describe the band this
> document was written against; at 10 bps round trip the 100/50 break-even is 40.0%.
> The method is unchanged.

# How to review the guard experiment

Use this after running the arena with the churn guard and the exit floor armed for two
to three hours. It produces one scorecard and one verdict — stop `scalper-perp`, keep
running it, or admit the window was too short to say.

The decision rules are enforced by `arena.experiment.verdict`, so the verdict is
reproducible rather than a matter of reading the table kindly.

## What this window can answer

Two different questions get confused here, and only one of them has an answer in three
hours.

**It can answer "are the guards working, and is the target reachable?"** Both are
near-binary facts that show up in a handful of observations. A position either touches
+50 bps at some point during its hold or it does not.

**It cannot answer "is this profitable?"** Break-even on the 50/25 band with a 10 bps
round trip is 46.7%. At 60 round trips, a 95% interval around a 50% win rate still
spans roughly 35–63%, so the break-even sits *inside* it. Two hours yields six to nine
round trips. A verdict of "profitable" or "unprofitable" from that is a coin flip
wearing a lab coat.

So the short window is a **screening test**, and the scorecard says so out loud instead
of inventing a p-value. Profitability is a second gate, listed at the end.

## Before you start

The guards install at startup. A process that began before this change has none, and
its fills say nothing about them.

1. Stop the running bot and agent, then start them again:

   ```bash
   uv run python manage_bot.py stop
   uv run python manage_bot.py start backend
   uv run python -m deploy.agent --from-config scalper-perp \
       --bootstrap-servers localhost:9092
   ```

2. Confirm all three banners. If any line is missing, stop here — the run is not an
   experiment.

   ```bash
   grep -E "Churn guard:|Exit floor:|Hold watchdog:" .run/bot.log | tail -3
   ```

   ```
   Churn guard: 6 fills/hour, 60 s re-entry cooldown, 0.25 % of equity/hour in fees (reducing a position is never blocked)
   Exit floor:  50  bps profit target, 25  bps stop, 3600 s hold limit (closes between the two are refused)
   Hold watchdog: closes anything held past 3600 s, checking every 30 s
   ```

3. Write down the bot's start time — the moment `manage_bot.py start backend` was run.
   The banners are printed then, and every later command needs that time.

## What the guards do now

The exit floor permits a close past the 60-minute limit but nothing compels one: in the
measured run four round trips sat 800–3100 s past the limit because the only actor that
could submit the exit was an LLM on a market tick. `HoldWatchdog` now submits those
closes every 30 s through the normal order path — every gate that judges an agent's
order judges the watchdog's, and a reduce is never refused for churn. An add of dust no
longer renews the hold clock, so `trapped` now means the watchdog did not fire.

The dust rule changes what the scorecard can show: a perp flip whose opening leg is
below the venue minimum notional is booked as the close it mostly was, and an exit is
never refused for being small — so a trapped position can no longer be a $0.92 residue
nobody can flatten.

## Run the scorecard

After two hours, and again at three:

```bash
uv run python -m scripts.experiment_report --since 2026-11-26T13:00:00
```

`--since` must be at or before the **bot's** start time. The banners are printed by the
bot at boot and carry no timestamp of their own; the report attributes them to the last
timestamped line above them. The agent starts about 30 s after the bot, and a `--since`
taken from the agent's start time falls after those banner lines — `_guards_armed` then
misses them and the report says `NOT ARMED` for a run that was armed. Use the bot's
start time.

It reads `data/arena.db`, `data/snapshots_*.csv` and `.run/bot.log`. It writes nothing.
Exit code is `0` for continue or inconclusive, `2` for stop, `3` for not armed.

## Read the verdict

| Verdict | Meaning | Do this |
|---|---|---|
| `NOT ARMED` | No guard banner in the window. The process predates the guards. | Restart, new start time, run again. The numbers above it are the old behaviour. |
| `STOP` — guard did not fire | A close landed inside the band, a position sat past the hold limit, or fee burn exceeded the budget. | This is a bug, not a strategy result. Nothing else in the run is evidence. Fix and rerun. |
| `STOP` — target not reachable | Six or more round trips and not one came within 50 bps of profit at its best moment. | Stop `scalper-perp`. No win rate fixes an unreachable target. |
| `STOP` — loss beyond fees | Net loss is more than twice the fees paid. | Stop. The entries and exits are wrong, not just expensive. |
| `CONTINUE` | Mechanics clean, at least two targets reached, net P&L positive. | Keep running into the second gate below. |
| `INCONCLUSIVE` | Fewer than six round trips, or a result between the two. | Let it run longer. Do not decide. |

## What each scorecard line means

| Line | What it is | What to look for |
|---|---|---|
| `fills`, `fills/hour` | Fills booked in the window | At or under `max_fills_per_hour` (6). Higher means the guard is not binding. |
| `fees`, `%/hour` | Fees paid, and as a share of equity per hour | Under `max_fee_burn_pct_per_hour` (0.25%). At the 6-fill cap on 25% positions the true rate is about 0.075%/hour, so anywhere near 0.25% means the cap is leaking. |
| `reached target (MFE)` | Round trips whose best moment reached +50 bps | The decisive number. `0/N` with `N ≥ 6` is a stop. |
| `hit stop` | Round trips that ended at −25 bps or worse | Not a failure — a stop doing its job. |
| `inside band` | Closes that landed between the stop and the target before the hold limit | Must be `0`. Anything else means the exit floor was bypassed. |
| `trapped past cap` | Positions held more than 5 minutes past the 3600s limit | Must be `0`. A nonzero value is a watchdog bug, not a strategy result: the process started before the watchdog existed, the loop crashed, or the close was refused every tick (log line `hold watchdog could not close ...`). |
| `median MFE` per exit kind | How far positions actually travelled | If `escape` rows show a median MFE near zero, the read is not finding direction at this horizon. |
| `mean/sd per trip` | Descriptive ratio | Context only. At this sample size it is not an estimate of an edge, and the verdict ignores it. |

## If it says INCONCLUSIVE

Run to six round trips at minimum; three round trips an hour puts that at about two
hours, nine at three. If the window closes with fewer, extend rather than decide.

## The second gate: profitability

Only after `CONTINUE`. This one needs **60–100 round trips**, which at three an hour is
two to three days of running.

At that size, compare the win rate against 46.7% with its interval rather than against
your impression:

| Round trips | 95% CI width at a 50% win rate |
|---|---|
| 20 | 40 points |
| 45 | 28 points |
| 60 | 24 points |
| 100 | 19 points |

Even at 100 trades the interval spans about 40–60%, so the honest test is not "did it
beat 46.7%" but "does the interval sit entirely above 46.7%". Getting there needs
roughly 150–400 round trips depending on how large the true edge is — 50 to 130 hours
of trading. Decide that with that number in hand, not with two hours of intuition.

If you only want a coarse answer, the cheaper question is the one the short window
already answers: **did positions ever reach +50 bps?** If the median MFE on escaped
positions stays in single-digit bps, the read has no forward edge at this horizon and no
amount of extra samples will change that.
