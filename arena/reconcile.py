"""Startup reconciliation between the wallet and the venue, and equity sync.

Two different questions, deliberately answered by two different rules:

- **Reconciliation** asks "does the ledger describe the wallet?". A *shortfall* — the
  ledger promising a quantity or a balance that the venue does not have — blocks live
  ordering, because every later decision would be sized against money that is not
  there. A *surplus* cannot lose money on its own, so it is reported rather than
  treated as a fault.
- **Equity sync** asks "did someone put money in or take it out?". A deposit is an
  event, not a disagreement: adopting it is how a hand-made top-up lifts the
  percentage caps without a restart. It is adopted only once it has held steady across
  two samples (a single reading could be a stale response), and every adoption is
  written to `equity_events` so the equity curve stays explainable.

A withdrawal is *not* symmetric: it looks exactly like a bug that invented money, so it
requires an explicit `--write-down` by the operator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from arena.instruments import SymbolRules

log = logging.getLogger(__name__)

DEFAULT_QUOTE_ASSET = "USDT"
# Quote-amount fuzz below which a difference is rounding, not money.
QUOTE_EPSILON = 1e-6


@dataclass(frozen=True)
class Drift:
    """One difference between the ledger and the venue."""

    kind: str  # shortfall | surplus | unaligned_open_orders | missing_baseline
    subject: str  # symbol, asset, or a marker like "open_orders"
    expected: float
    actual: float
    detail: str


@dataclass(frozen=True)
class ReconcileReport:
    blocking: tuple[Drift, ...] = ()
    notes: tuple[Drift, ...] = ()
    balances: dict[str, float] = field(default_factory=dict)
    baseline: dict[str, float] = field(default_factory=dict)
    ledger_cash: float = 0.0
    ledger_positions: dict[str, float] = field(default_factory=dict)
    quote_asset: str = DEFAULT_QUOTE_ASSET

    @property
    def armed(self) -> bool:
        return not self.blocking

    @property
    def quote_surplus(self) -> float:
        """Extra quote balance the ledger does not account for (a deposit candidate)."""
        return sum(d.actual - d.expected for d in self.notes if d.kind == "surplus"
                   and d.subject == self.quote_asset)

    def first_blocker(self) -> str:
        return self.blocking[0].detail if self.blocking else ""

    def summary(self) -> str:
        if self.armed:
            head = "reconciled: ledger matches the venue"
        else:
            head = f"NOT reconciled: {len(self.blocking)} blocking difference(s)"
        lines = [head]
        for drift in self.blocking:
            lines.append(f"  BLOCK  {drift.detail}")
        for drift in self.notes:
            lines.append(f"  note   {drift.detail}")
        return "\n".join(lines)


def compare(
    *,
    balances: dict[str, float],
    baseline: dict[str, float],
    ledger_positions: dict[str, float],
    ledger_cash: float,
    rules: dict[str, SymbolRules],
    quote_asset: str = DEFAULT_QUOTE_ASSET,
    reserve_usdt: float = 0.0,
    open_orders: int = 0,
) -> ReconcileReport:
    """Compare the venue's balances with the ledger's claims.

    ``baseline`` is what already sat in the wallet before go-live: without it, holdings
    the arena never bought would be reported as drift forever, and a surplus would be
    mistaken for a deposit.
    """
    blocking: list[Drift] = []
    notes: list[Drift] = []

    if not baseline:
        blocking.append(
            Drift(
                kind="missing_baseline",
                subject="baseline",
                expected=0.0,
                actual=0.0,
                detail=(
                    "no go-live baseline recorded, so pre-existing holdings cannot be "
                    "separated from arena positions; run `manage_bot.py live-init`"
                ),
            )
        )

    for symbol, expected in sorted(ledger_positions.items()):
        rule = rules.get(symbol)
        if rule is None:
            continue  # a paper-only symbol: the venue knows nothing about it
        base = rule.base_asset
        actual = balances.get(base, 0.0) - baseline.get(base, 0.0)
        # One step of tolerance: the venue truncates to its own grid, so an exact float
        # comparison would report drift on every position it holds.
        tolerance = float(Decimal(str(rule.step_size)))
        if actual < expected - tolerance:
            blocking.append(
                Drift(
                    kind="shortfall",
                    subject=symbol,
                    expected=expected,
                    actual=actual,
                    detail=(
                        f"{base}: the ledger holds {expected:g} but the wallet has "
                        f"{actual:g} (short {expected - actual:g}) — the arena cannot sell "
                        f"what is not there"
                    ),
                )
            )
        elif actual > expected + tolerance:
            notes.append(
                Drift(
                    kind="surplus",
                    subject=symbol,
                    expected=expected,
                    actual=actual,
                    detail=(
                        f"{base}: the wallet holds {actual - expected:g} more than the "
                        f"ledger ({actual:g} vs {expected:g}); not adopted — quote "
                        f"deposits are what equity sync handles"
                    ),
                )
            )

    # Symbols the arena bought but the rules no longer cover would silently escape the
    # comparison above; say so instead of implying a clean reconcile.
    uncovered = sorted(set(ledger_positions) - set(rules))
    if uncovered:
        notes.append(
            Drift(
                kind="uncovered",
                subject="rules",
                expected=0.0,
                actual=0.0,
                detail=(
                    f"no venue rules for {', '.join(uncovered)}; their balances were not "
                    f"compared (they trade on the legacy paper rule)"
                ),
            )
        )

    actual_quote = balances.get(quote_asset, 0.0) - baseline.get(quote_asset, 0.0)
    spendable = actual_quote - reserve_usdt
    if ledger_cash > spendable + QUOTE_EPSILON:
        blocking.append(
            Drift(
                kind="shortfall",
                subject=quote_asset,
                expected=ledger_cash,
                actual=spendable,
                detail=(
                    f"{quote_asset}: the ledger allocates {ledger_cash:,.2f} but only "
                    f"{spendable:,.2f} is free (reserve {reserve_usdt:,.2f} excluded) — "
                    f"short {ledger_cash - spendable:,.2f}"
                ),
            )
        )
    elif spendable > ledger_cash + QUOTE_EPSILON:
        notes.append(
            Drift(
                kind="surplus",
                subject=quote_asset,
                expected=ledger_cash,
                actual=spendable,
                detail=(
                    f"{quote_asset}: {spendable - ledger_cash:,.2f} is unallocated "
                    f"(wallet {spendable:,.2f} vs ledger {ledger_cash:,.2f})"
                ),
            )
        )

    if open_orders:
        blocking.append(
            Drift(
                kind="unaligned_open_orders",
                subject="open_orders",
                expected=0.0,
                actual=float(open_orders),
                detail=(
                    f"{open_orders} open order(s) at the venue; this bot only places "
                    f"market orders, so they were not placed by it — cancel them before "
                    f"arming"
                ),
            )
        )

    return ReconcileReport(
        blocking=tuple(blocking),
        notes=tuple(notes),
        balances=dict(balances),
        baseline=dict(baseline),
        ledger_cash=ledger_cash,
        ledger_positions=dict(ledger_positions),
        quote_asset=quote_asset,
    )


# ── Equity sync (deposits) ───────────────────────────────────────


@dataclass(frozen=True)
class Adoption:
    """A confirmed deposit, split across the agents that may trade."""

    amount: float
    allocations: dict[str, float]
    note: str


class EquitySync:
    """Adopt a stable quote surplus into the agents' cash.

    Two samples, not one: a single reading can be a cached or partially settled venue
    response, and crediting money that is not there is the one direction that makes the
    ledger lie about what it can spend.
    """

    def __init__(
        self,
        *,
        policy: str = "credit_sole_agent",
        tolerance_usdt: float = 1.0,
        mapped_agents: tuple[str, ...] = (),
    ) -> None:
        if policy not in ("credit_sole_agent", "proportional", "unallocated"):
            raise ValueError(f"unknown deposit policy {policy!r}")
        self.policy = policy
        self.tolerance = tolerance_usdt
        self.mapped_agents = tuple(mapped_agents)
        self._last_surplus: float | None = None

    def observe(self, report: ReconcileReport, equity_by_agent: dict[str, float]) -> Adoption | None:
        """Return an adoption when the surplus has held steady, else record it."""
        surplus = report.quote_surplus
        if surplus <= self.tolerance:
            self._last_surplus = None
            return None

        if self._last_surplus is None or abs(surplus - self._last_surplus) > self.tolerance:
            # First sighting (or it moved): wait for the next sample to agree.
            self._last_surplus = surplus
            return None

        self._last_surplus = None
        return self._adoption(surplus, equity_by_agent)

    def _adoption(self, surplus: float, equity_by_agent: dict[str, float]) -> Adoption:
        agents = [a for a in self.mapped_agents if a in equity_by_agent]
        if self.policy == "unallocated" or not agents:
            return Adoption(
                amount=surplus,
                allocations={},
                note="left unallocated: no agent may spend it until `manage_bot.py allocate`",
            )
        if self.policy == "credit_sole_agent":
            if len(agents) != 1:
                return Adoption(
                    amount=surplus,
                    allocations={},
                    note=(
                        f"left unallocated: {len(agents)} agents trade this account, so "
                        f"credit_sole_agent is ambiguous"
                    ),
                )
            return Adoption(amount=surplus, allocations={agents[0]: surplus}, note="credited")

        total = sum(max(equity_by_agent[a], 0.0) for a in agents)
        if total <= 0:
            return Adoption(
                amount=surplus,
                allocations={},
                note="left unallocated: every agent's equity is zero",
            )
        allocations = {
            agent: surplus * max(equity_by_agent[agent], 0.0) / total for agent in agents
        }
        return Adoption(amount=surplus, allocations=allocations, note="credited proportionally")
