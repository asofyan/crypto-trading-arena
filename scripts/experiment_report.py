"""Print the guard-experiment scorecard and its stop/continue verdict.

    uv run python -m scripts.experiment_report --since 2026-11-26T13:00:00

Reads ``data/arena.db`` (fills), ``data/snapshots_*.csv`` (prices for MFE/MAE) and
``.run/bot.log`` (guard refusals). Nothing here changes state: it is a report.

The decision rules live in ``arena.experiment.verdict`` and are documented for the
operator in ``docs/experiments/guard-review.md``.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from arena.experiment import (
    DEFAULT_HOLD_LIMIT_S,
    DEFAULT_STOP_BPS,
    DEFAULT_TARGET_BPS,
    RoundTrip,
    Scorecard,
    classify_exit,
    excursion,
    sharpe_like,
    verdict,
)

REPO = Path(__file__).resolve().parent.parent
_DENY = re.compile(r"REJECT \((?P<kind>[^)]+)\)")
# The startup banners ``deploy.tools_and_dashboard`` prints when it installs the guards.
# Their absence is not a strategy result: it means the run predates the guards.
_BANNERS = ("Churn guard:", "Exit floor:")


def _guards_armed(log_path: Path, since: float) -> bool:
    """Did a process that started after ``since`` print both guard banners?

    The banners come from ``print()``, so they carry no timestamp of their own; they are
    attributed to the last timestamped line above them. Without that, every banner looks
    like it belongs to no moment at all and a correctly armed run reports as unarmed.
    """
    if not log_path.exists():
        return False
    armed = dict.fromkeys(_BANNERS, False)
    current = 0.0
    for line in log_path.read_text(errors="replace").splitlines():
        try:
            current = _ts(line[:19])
            continue
        except ValueError:
            pass
        if current < since:
            continue
        for banner in _BANNERS:
            if banner in line:
                armed[banner] = True
    return all(armed.values())


def _ts(value: str) -> float:
    """A DB timestamp (UTC-aware) or a CSV timestamp (local naive) as epoch seconds."""
    dt = datetime.fromisoformat(value)
    return dt.timestamp() if dt.tzinfo else dt.replace().timestamp()


def _snapshots(data_dir: Path) -> dict[str, list[tuple[float, float]]]:
    """``product_id -> [(epoch, market_price)]`` from every snapshot CSV on disk."""
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for path in sorted(data_dir.glob("snapshots_*.csv")):
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                product = (row.get("product_id") or "").strip()
                price = (row.get("market_price") or "").strip()
                if not product or not price:
                    continue
                try:
                    series[product].append((_ts(row["timestamp"]), float(price)))
                except (KeyError, ValueError):
                    continue
    for values in series.values():
        values.sort()
    return series


def _round_trips(fills: list[dict], prices, target, stop, hold_limit) -> list[RoundTrip]:
    """Rebuild closed positions from the fill stream, the way the store booked them.

    Net position, average entry and the holding clock are tracked exactly as
    ``AccountStore`` tracks them, so a flip is scored as a close plus a fresh open
    rather than as one confusing fill.
    """
    trips: list[RoundTrip] = []
    pos: dict[str, float] = {}
    basis: dict[str, float] = {}  # signed notional, per unit * qty
    entry_ts: dict[str, float] = {}
    entry_fee_per_unit: dict[str, float] = {}

    for fill in fills:
        product = fill["product_id"]
        price = float(fill["price"])
        qty = float(fill["quantity"])
        fee = float(fill["fee"])
        delta = qty if fill["action"] == "buy" else -qty
        held = pos.get(product, 0.0)
        if abs(held) <= 1e-9:
            held = 0.0

        closing = held != 0.0 and (held > 0) != (delta > 0)
        closed = min(abs(delta), abs(held)) if closing else 0.0
        if closed:
            entry_price = abs(basis[product]) / abs(held)
            exit_fee = fee * (closed / abs(delta)) if delta else 0.0
            exit_ts = fill["ts"]
            start = entry_ts.get(product, exit_ts)
            direction = 1 if held > 0 else -1
            window = [p for t, p in prices.get(product, ()) if start <= t <= exit_ts]
            mfe, mae = excursion(window + [price], entry_price, direction)
            unit_fee = entry_fee_per_unit.get(product, 0.0)
            gross = closed * (price - entry_price) * direction
            trips.append(
                RoundTrip(
                    product_id=product,
                    entry_ts=start,
                    exit_ts=exit_ts,
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=price,
                    net_pnl=gross - closed * unit_fee - exit_fee,
                    fees=closed * unit_fee + exit_fee,
                    hold_s=max(0.0, exit_ts - start),
                    mfe_bps=mfe,
                    mae_bps=mae,
                )
            )

        # Advance the book exactly like the store: the closed part keeps the old basis,
        # the remainder is re-based at this fill's price.
        closed_signed = (-closed if held > 0 else closed) if closing else 0.0
        remainder = delta - closed_signed
        old_entry = abs(basis[product]) / abs(held) if held else price
        new_held = held + delta
        if abs(new_held) <= 1e-9:
            pos.pop(product, None)
            basis.pop(product, None)
            entry_ts.pop(product, None)
            entry_fee_per_unit.pop(product, None)
            continue
        pos[product] = new_held
        basis[product] = (held + closed_signed) * old_entry * (1 if held >= 0 else -1)
        basis[product] += remainder * price
        if closing and closed >= abs(held) - 1e-9:
            entry_ts[product] = fill["ts"]
            entry_fee_per_unit[product] = fee / abs(delta) if delta else 0.0
        elif not closing:
            opened = abs(held) * entry_fee_per_unit.get(product, 0.0) + fee
            entry_ts.setdefault(product, fill["ts"])
            entry_fee_per_unit[product] = opened / abs(new_held)
    return trips


def _denials(log_path: Path, since: float) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    if not log_path.exists():
        return counts
    current = 0.0
    for line in log_path.read_text(errors="replace").splitlines():
        try:
            current = _ts(line[:19])
        except ValueError:
            pass  # banner or wrapped line: keep the timestamp it sits under
        match = _DENY.search(line)
        if not match or current < since:
            continue
        counts[match.group("kind").strip()] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        required=True,
        help="Experiment start, ISO-8601 (e.g. 2026-11-26T13:00:00). Only later fills count.",
    )
    parser.add_argument("--db", default=str(REPO / "data" / "arena.db"))
    parser.add_argument("--data-dir", default=str(REPO / "data"))
    parser.add_argument("--log", default=str(REPO / ".run" / "bot.log"))
    parser.add_argument("--agent", default="scalper-perp")
    parser.add_argument("--target-bps", type=float, default=DEFAULT_TARGET_BPS)
    parser.add_argument("--stop-bps", type=float, default=DEFAULT_STOP_BPS)
    parser.add_argument("--hold-limit", type=float, default=DEFAULT_HOLD_LIMIT_S)
    args = parser.parse_args(argv)

    since = _ts(args.since) if "T" in args.since else _ts(args.since + "T00:00:00")
    data_dir = Path(args.data_dir)

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT ts, action, product_id, quantity, price, fee FROM fills "
        "WHERE agent_id = ? ORDER BY ts",
        (args.agent,),
    ).fetchall()
    fills = [
        {
            "ts": _ts(r["ts"]),
            "action": r["action"],
            "product_id": r["product_id"],
            "quantity": r["quantity"],
            "price": r["price"],
            "fee": r["fee"],
        }
        for r in rows
        if _ts(r["ts"]) >= since
    ]
    if not fills:
        print(f"No fills for {args.agent} after {args.since}. Nothing to score yet.")
        return 1

    prices = _snapshots(data_dir)
    trips = _round_trips(fills, prices, args.target_bps, args.stop_bps, args.hold_limit)
    denials = _denials(Path(args.log), since)

    window = fills[-1]["ts"] - since
    equity = 100.0
    for path in sorted(data_dir.glob("snapshots_*.csv")):
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    if _ts(row["timestamp"]) >= since and float(row["portfolio_value"]):
                        equity = float(row["portfolio_value"])
                        break
                except (KeyError, ValueError):
                    continue
            else:
                continue
            break

    card = Scorecard(
        window_s=window,
        equity=equity,
        fills=len(fills),
        round_trips=trips,
        denials_churn=denials.get("churn", 0),
        denials_exit_floor=denials.get("exit floor", 0),
        target_bps=args.target_bps,
        stop_bps=args.stop_bps,
        hold_limit_s=args.hold_limit,
    )

    print()
    print("GUARD EXPERIMENT SCORECARD".center(74))
    print("=" * 74)
    print(f"  window            {window / 3600:.2f}h   equity ${card.equity:.2f}")
    print(f"  fills             {card.fills}  = {card.fill_rate_per_hour:.2f}/hour")
    print(f"  round trips       {len(trips)}")
    print(f"  fees              ${card.total_fees:.4f} = {card.fee_pct_per_hour:.3f}%/hour")
    print(f"  net P&L           ${card.net_pnl:+.4f}")
    print(f"  refusals          churn {card.denials_churn}  exit floor {card.denials_exit_floor}")
    print()
    print(f"  band              target +{card.target_bps:g} bps | stop -{card.stop_bps:g} bps | "
          f"escape {card.hold_limit_s:g}s")
    print(f"  reached target    {card.target_hits}/{len(trips)}  (MFE >= target)")
    print(f"  hit stop          {card.stop_hits}/{len(trips)}")
    print(f"  inside band       {card.inside_band}   <- must be 0")
    print(f"  trapped past cap  {card.trapped}   <- must be 0")
    ratio = sharpe_like(card)
    if ratio is not None:
        print(f"  mean/sd per trip  {ratio:+.2f}   (descriptive only, not an edge estimate)")
    if trips:
        print()
        print("  exit     count  median bps   median hold   median MFE")
        for reason in ("target", "stop", "escape", "inside-band"):
            group = [
                t
                for t in trips
                if classify_exit(
                    t.bps,
                    t.hold_s,
                    target_bps=card.target_bps,
                    stop_bps=card.stop_bps,
                    hold_limit_s=card.hold_limit_s,
                )
                == reason
            ]
            if not group:
                continue
            med = sorted(g.bps for g in group)[len(group) // 2]
            hold = sorted(g.hold_s for g in group)[len(group) // 2]
            mfe = sorted(g.mfe_bps for g in group)[len(group) // 2]
            print(f"  {reason:10s} {len(group):5d}  {med:+10.1f}   {hold:9.0f}s   {mfe:+9.1f}")

    armed = _guards_armed(Path(args.log), since)
    decision, reasons = verdict(card)
    if not armed:
        decision = "not armed"
        reasons = [
            "No 'Churn guard:' and 'Exit floor:' banner appeared in this window, so the "
            "process never installed the guards. Every number above is the old "
            "behaviour and is not evidence about the guards. Restart the bot (see the "
            "review guide) and run this again with the new start time."
        ]
    print()
    print("=" * 74)
    print(f"  VERDICT: {decision.upper()}")
    print("=" * 74)
    for reason in reasons:
        for sentence in reason.split(". "):
            text = sentence.strip()
            if text and not text.endswith("."):
                text += "."
            if text:
                print(f"  - {text}")
    print()
    return {"stop": 2, "not armed": 3}.get(decision, 0)


if __name__ == "__main__":
    sys.exit(main())
