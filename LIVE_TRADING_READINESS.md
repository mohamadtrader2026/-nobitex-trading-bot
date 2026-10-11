# Live Trading Readiness Review (Preparation Only)

**Status: NOT APPROVED FOR LIVE TRADING. No live orders are enabled by this document.**

This checklist is a preparation artifact only. It does not change runtime trading behavior, configure credentials, or submit orders.

## Explicit constraints for this preparation

- Real order submission must remain disabled during preparation and testing.
- Do not request, store, or commit an API token in source code, issues, pull requests, logs, or chat.
- The maximum notional amount per trade is **UNSET**. Do not infer a value from the paper-trading default.
- Until a maximum is deliberately chosen and enforced in code, live trading must remain blocked.
- If any step's outcome is uncertain (including order timeout), stop new entries and reconcile against the exchange before resuming.

## Required engineering gates before any live-trading review

- [x] Add a fail-closed live-mode guard: TEST MODE remains the default, live orders need separate explicit opt-ins, and missing/invalid risk configuration prevents order submission. (Implemented; the compile, safe-unit, offline-backtest, and paper-trading CI jobs passed on the preceding code commit.)
- [x] Require an explicit positive whole-Rial maximum for new buy exposure and reject buys above the cap including estimated fees. The cap remains UNSET by default; sell exits are not blocked by the entry cap. (Implemented; the compile, safe-unit, offline-backtest, and paper-trading CI jobs passed on the preceding code commit.)
- [ ] Validate available balance, market minimum/step size, symbol support, and order precision before submitting.
- [ ] On startup, reconcile exchange balances, open orders, and recent fills with persisted bot state. Do not assume an empty local state means no position exists.
- [ ] Make order submission idempotent where supported; on uncertain outcomes, halt entries and reconcile instead of retrying blindly.
- [ ] Test partial fills, rejected orders, rate limits, network failures, timeouts, duplicate runs, corrupted state, and restarts.
- [ ] Review stop-loss / take-profit behavior and explicitly document that in-process checks cannot protect a position while the process is down. Evaluate exchange-side protective orders if supported.
- [ ] Ensure a daily loss limit, maximum exposure, and emergency stop are enforced independently of strategy signals.
- [ ] Keep API credentials in GitHub Actions Secrets or an equivalent secret manager. Use least privilege; disable withdrawals.
- [ ] Add monitoring and alerts for crashes, stale market data, repeated API errors, halted state, and unexpected open exposure.
- [ ] Run unit tests, paper trading, and a backtest with real historical data and account-specific fees/slippage assumptions. New mocked tests cover the live-order fail-closed guard; the compile, safe-unit, offline-backtest, and paper-trading CI jobs passed on the preceding code commit.
- [ ] Review all changes in a pull request and verify that CI is green before any separate authorization to consider live mode.

## Current known limitations to resolve

- The README documents mainnet/test-mode environment variables, but configuration alone is not a safety review.
- Strategy stop-loss/take-profit checks performed inside the process do not run if the process is stopped.
- Persisted local state can diverge from exchange state after a crash or uncertain order response.
- Paper LONG/SHORT simulation does not place real orders and does not establish support for real spot shorting.
- GitHub Actions and Codespaces do not guarantee uninterrupted 24/7 execution.

## Safe validation policy

All tests in this preparation must use mocks, recorded responses, or paper mode. No test should call the live order-submission endpoint. A future live-trading decision requires a separate explicit review after all gates are implemented and tested.
