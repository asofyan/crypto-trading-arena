# Live perpetuals use a dedicated futures wallet

**Status:** accepted. Extends ADR-0004 to Binance USDⓈ-M and supersedes ADR-0008's paper-only restriction.

## Decision

Live perpetual execution owns one Binance USDⓈ-M wallet and maps it to one perp-only agent. Binance one-way mode matches the arena's signed net position. The wallet uses single-asset USDT, cross margin, and 1× leverage. A futures ledger is bound to its Binance account, environment, and agent.

The venue owns position quantity, execution price, commission, and USDT wallet balance. The arena owns order attribution and its own cash representation. To reconcile wallet principal, the runtime adds signed cost basis to ledger cash. When adding exposure, the runtime checks both its own gross exposure cap and the venue's available margin.

The tool server routes reductions to market orders with Binance `reduceOnly`. It journals an intent before sending it. When the outcome is uncertain, order lookup uses the original client order ID and the session disarms while it lacks proof of the outcome. Venue position, margin configuration, and open order checks gate startup.

## Consequences

A single execution runtime serves either spot or perpetuals. The operator uses a separate ledger for Demo and production. Each perpetual symbol has one owner, so a venue reduction can be attributed to a single agent. The tool signatures stay shared across modes.

Reconciliation records authenticated funding when it accounts for the wallet difference. An unexplained wallet change, external position, or open order blocks arming. The operator must investigate the discrepancy and leave the ledger available for order recovery.
