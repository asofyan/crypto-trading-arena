"""Score a TP/SL band against what the market actually did.

The band is the one decision a minute-horizon strategy cannot dodge: its EV is
``p·TP − (1−p)·SL − C``, and on a driftless random walk ``p = SL/(TP+SL)`` so the EV is
exactly ``−C``. A band can therefore only pay if the strategy's hit rate beats the
random-walk one — and that is a number you can measure, not argue about.

This script replays a recorded session: every entry from ``trades_*.csv`` is handed the
quote path from ``ticks_*.csv`` and asked which barrier it touched first inside the hold
limit. It reports the observed hit rate next to the rate the configured cost requires,
so "does this band work on this pair" becomes a measurement.

Usage:
    uv run python -m scripts.band_audit --data-dir data \
        --tp-bps 100 --sl-bps 50 --hold-minutes 60 --fee-bps 10
"""

from __future__ import annotations

import argparse
import csv
import glob
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_FEE_BPS = 10.0


# ── Loading ──────────────────────────────────────────────────────


def load_ticks(paths: list[str]) -> dict[str, list[tuple[datetime, float, float]]]:
    """Quote path per product: (time, best_bid, best_ask) in file order."""
    ticks: dict[str, list[tuple[datetime, float, float]]] = defaultdict(list)
    for path in paths:
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    ticks[row["product_id"]].append(
                        (
                            datetime.fromisoformat(row["timestamp"]),
                            float(row["best_bid"]),
                            float(row["best_ask"]),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
    for series in ticks.values():
        series.sort(key=lambda t: t[0])
    return ticks


def load_fills(paths: list[str]) -> list[dict]:
    fills = []
    for path in paths:
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                fills.append(
                    {
                        "t": datetime.fromisoformat(row["timestamp"]),
                        "agent": row.get("agent_id", ""),
                        "product": row["product_id"],
                        "qty": float(row["quantity"]),
                        "price": float(row["price"]),
                        "sign": 1 if row["action"] == "buy" else -1,
                        "fee": float(row["fee"]),
                    }
                )
    fills.sort(key=lambda f: f["t"])
    return fills


# ── Episodes (flat -> flat, flips split) ─────────────────────────


def _weighted(pairs: list[tuple[float, float]]) -> float:
    return sum(p * q for p, q in pairs) / sum(q for _, q in pairs)


def episodes(fills: list[dict]) -> list[dict]:
    """Reconstruct flat-to-flat position episodes, splitting fills that flip the side."""
    out: list[dict] = []
    # Keyed by (agent, product): two agents trading the same product hold separate
    # positions, and merging them invents an entry price neither ever paid.
    pos: dict[tuple[str, str], float] = defaultdict(float)
    live: dict[tuple[str, str], dict] = {}

    def close(key: tuple[str, str], when: datetime) -> None:
        ep = live.pop(key)
        out.append(
            {
                "product": key[1],
                "agent": ep["agent"],
                "side": ep["side"],
                "entry": _weighted(ep["entry"]),
                "exit": _weighted(ep["exit"]) if ep["exit"] else None,
                "t_open": ep["t_open"],
                "t_close": when,
                "held_s": (when - ep["t_open"]).total_seconds(),
            }
        )

    for fill in fills:
        product, qty, price, sign = fill["product"], fill["qty"], fill["price"], fill["sign"]
        key = (fill["agent"], product)
        before = pos[key]

        if abs(before) < 1e-9:
            live[key] = {
                "agent": fill["agent"],
                "side": sign,
                "t_open": fill["t"],
                "entry": [(price, qty)],
                "exit": [],
            }
            pos[key] = sign * qty
            continue

        if before * sign > 0:
            live[key]["entry"].append((price, qty))
            pos[key] = before + sign * qty
            continue

        have = abs(before)
        closed = min(qty, have)
        live[key]["exit"].append((price, closed))

        if qty > have:  # flipped through zero: close here, open the remainder
            close(key, fill["t"])
            remainder = qty - have
            live[key] = {
                "agent": fill["agent"],
                "side": sign,
                "t_open": fill["t"],
                "entry": [(price, remainder)],
                "exit": [],
            }
            pos[key] = sign * remainder
        else:
            pos[key] = before + sign * qty
            if abs(pos[key]) < 1e-9:
                close(key, fill["t"])

    return out


# ── Band scoring ─────────────────────────────────────────────────


@dataclass
class Outcome:
    product: str
    side: int
    result: str  # "tp" | "sl" | "hold"
    bps: float  # realised move at the exit the strategy would have taken
    mfe_bps: float
    mae_bps: float
    held_s: float


def score_episode(ep: dict, quotes: list[tuple[datetime, float, float]], tp: float, sl: float,
                  hold_seconds: float) -> Outcome | None:
    """Walk the quotes while the position was open; report the first barrier touched.

    Barriers are measured on the touch a position can actually be closed at: a long
    exits into the bid, a short into the ask. Optimistic mid-price barriers are how a
    backtest lies.
    """
    if not quotes:
        return None
    window = [q for q in quotes if ep["t_open"] <= q[0] <= ep["t_open"] + timedelta(seconds=hold_seconds)]
    if not window:
        return None

    entry = ep["entry"]
    side = ep["side"]
    exit_px = (lambda q: q[1]) if side > 0 else (lambda q: q[2])
    mfe = mae = 0.0
    result, bps, held = "hold", 0.0, 0.0

    for when, bid, ask in window:
        move = (exit_px((when, bid, ask)) / entry - 1) * 1e4 * side
        mfe, mae = max(mfe, move), min(mae, move)
        # Quote sampling cannot order two touches inside one interval, so the stop wins:
        # overstating stops is the safe direction for a number that sizes a band.
        if move <= -sl:
            result, bps, held = "sl", -sl, (when - ep["t_open"]).total_seconds()
            break
        if move >= tp:
            result, bps, held = "tp", tp, (when - ep["t_open"]).total_seconds()
            break
    else:
        when, bid, ask = window[-1]
        bps = (exit_px((when, bid, ask)) / entry - 1) * 1e4 * side
        held = (when - ep["t_open"]).total_seconds()

    return Outcome(ep["product"], side, result, bps, mfe, mae, held)


def required_hit_rate(tp: float, sl: float, cost_bps: float) -> float:
    """``p`` at which EV = 0 for this band and cost: (SL + C) / (TP + SL)."""
    return (sl + cost_bps) / (tp + sl)


def summarise(outcomes: list[Outcome], tp: float, sl: float, cost_bps: float) -> dict:
    scored = list(outcomes)
    if not scored:
        return {"n": 0}
    hits = sum(1 for o in scored if o.result == "tp")
    stops = sum(1 for o in scored if o.result == "sl")
    holds = len(scored) - hits - stops
    ev = statistics.mean(o.bps - cost_bps for o in scored)
    return {
        "n": len(scored),
        "tp_pct": hits / len(scored) * 100,
        "sl_pct": stops / len(scored) * 100,
        "hold_pct": holds / len(scored) * 100,
        "required_pct": required_hit_rate(tp, sl, cost_bps) * 100,
        "ev_bps": ev,
        "median_mfe": statistics.median(o.mfe_bps for o in scored),
        "median_mae": statistics.median(o.mae_bps for o in scored),
        "median_held_min": statistics.median(o.held_s for o in scored) / 60,
    }


def render(rows: list[tuple[str, dict]], tp: float, sl: float, cost_bps: float) -> str:
    header = (
        f"band TP {tp:.0f} / SL {sl:.0f} bps, cost {cost_bps:.1f} bps, "
        f"required hit rate {required_hit_rate(tp, sl, cost_bps) * 100:.1f}%\n"
        f"{'product':18s} {'n':>3s} {'TP%':>6s} {'SL%':>6s} {'hold%':>6s} "
        f"{'EV bps':>7s} {'medMFE':>7s} {'medMAE':>7s} {'medHold':>8s}"
    )
    lines = [header, "-" * len(header.splitlines()[-1])]
    for product, stats in rows:
        if not stats.get("n"):
            lines.append(f"{product:18s}   0  (no quotes recorded for this position)")
            continue
        lines.append(
            f"{product:18s} {stats['n']:3d} {stats['tp_pct']:6.1f} {stats['sl_pct']:6.1f} "
            f"{stats['hold_pct']:6.1f} {stats['ev_bps']:7.1f} {stats['median_mfe']:7.1f} "
            f"{stats['median_mae']:7.1f} {stats['median_held_min']:7.1f}m"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.band_audit",
        description="Score a TP/SL band against a recorded session's quote path.",
    )
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tp-bps", type=float, default=100.0)
    parser.add_argument("--sl-bps", type=float, default=50.0)
    parser.add_argument("--hold-minutes", type=float, default=60.0)
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS,
                        help="round-trip taker fee; the spread is already inside the quotes")
    parser.add_argument("--ticks", nargs="*", default=None,
                        help="explicit ticks_*.csv files (default: every one in --data-dir)")
    parser.add_argument("--trades", nargs="*", default=None,
                        help="explicit trades_*.csv files (default: every one in --data-dir)")
    args = parser.parse_args(argv)

    ticks = args.ticks or sorted(glob.glob(str(Path(args.data_dir) / "ticks_*.csv")))
    trades = args.trades or sorted(glob.glob(str(Path(args.data_dir) / "trades_*.csv")))
    if not ticks:
        print("No ticks_*.csv found — start a session with CSV recording enabled first.")
        return 1
    if not trades:
        print("No trades_*.csv found — there is nothing to score.")
        return 1

    quotes = load_ticks(ticks)
    fill_path = load_fills(trades)
    if not fill_path:
        print("No fills in the trade logs.")
        return 1

    spreads = [
        (ask - bid) / ((ask + bid) / 2) * 1e4
        for series in quotes.values()
        for _, bid, ask in series
        if bid and ask
    ]
    spread_bps = statistics.median(spreads) if spreads else 0.0
    # Entry happens at the ask and the exit at the bid, so every scored move already
    # carries the spread; charging it again would double-count the crossing cost.
    cost = args.fee_bps

    outcomes = []
    for ep in episodes(fill_path):
        outcome = score_episode(ep, quotes.get(ep["product"], []), args.tp_bps, args.sl_bps,
                                args.hold_minutes * 60)
        if outcome is not None:
            outcomes.append(outcome)

    by_product: dict[str, list[Outcome]] = defaultdict(list)
    for outcome in outcomes:
        by_product[outcome.product].append(outcome)
    rows = [(product, summarise(items, args.tp_bps, args.sl_bps, cost))
            for product, items in sorted(by_product.items())]
    rows.append(("* all products", summarise(outcomes, args.tp_bps, args.sl_bps, cost)))

    print(f"ticks: {len(ticks)} file(s) | trades: {len(trades)} file(s) | "
          f"median spread {spread_bps:.2f} bps (already inside the quotes)")
    print(render(rows, args.tp_bps, args.sl_bps, cost))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
