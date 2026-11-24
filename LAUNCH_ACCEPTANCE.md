# Launch acceptance: FundingPips read-only MVP

How to run: the `backend-integration-tests` workflow runs on every PR (PostgreSQL 16 service container).
Locally: `TALITRADE_TEST_DATABASE_URL=postgresql+asyncpg://…/talitrade_test python -m pytest -q` from `backend/`.
The test database must have `test` or `disposable` in its name, because the app's tables are dropped.

Evidence for the results below:
- PR #148 CI run on commit `ea229e6`: **350 passed, 1 skipped** on PostgreSQL. The skip is a private bot database.
- Baseline probe: the same tests run against unfixed `main` (commit `76c00e3`, temporary branch since deleted) gave
  **5 failed, 345 passed**. Those 5 failures are the defects fixed in #148.

Status values: PASS (an automated test ran and passed) · FIXED (failed on `main`, passes with #148) ·
NOT TESTED (needs an external account or deployment access) · GAP (a known design gap, documented, not fixed).

| # | Requirement | Test (`backend/tests/…`) | Status | Remaining dependency |
|---|---|---|---|---|
| 1 | User A cannot read or modify User B's accounts | `test_fundingpips_e2e::test_two_users_are_isolated_end_to_end`, `…disconnect_then_reconnect…`, `…no_telegram_for_rejected_writes` | PASS | none |
| 2 | Extension tokens are scoped, revocable, stored hashed | `test_fundingpips_e2e::test_extension_token_is_hashed_scoped_and_revocable` | PASS | Browser storage of the token inside the extension is not covered |
| 3 | Re-ingestion does not duplicate trades | `…repeated_history_ingestion_does_not_duplicate` (PASS); `…trade_without_close_time_is_not_duplicated_on_resend` | FIXED | none |
| 4 | Closed positions are identified | `…flat_account_closes_all_positions_but_a_missing_list_does_not` | PASS | none |
| 5 | Partial closes do not corrupt the remaining positions | `…two_same_symbol_positions_survive_and_partial_close_keeps_the_other` | FIXED | When two identical positions are open, the server cannot tell which one closed. The extension sends no ticket ID (extension change needed) |
| 6 | Imported history reconciles | `…repeated_history_ingestion…` (journal sum = stored rows; stats total) | PASS | A live FundingPips history page is needed to validate the scraper's parsing |
| 7 | Balance and equity stay separate and accurate | `…missing_balance_and_equity_stay_null_and_are_kept_separate` | PASS (server) | **GAP**: when the page hides balance, the extension fills it from `accountSize + Σ closed P&L`, and the server cannot tell an estimate from an observed value |
| 8 | Missing values stay unknown | `…missing_pnl_is_rejected_not_stored_as_zero` | FIXED | none |
| 9 | Stale data is visibly identified | `…live_status_expires_when_heartbeats_stop` | PASS / **GAP** | `/extension/status` *drops* an account after 120 s instead of flagging it as stale. Live values are held in process memory only (lost on restart, not shared across replicas). The frontend must use `last_sync_at` |
| 10 | Dashboard totals reconcile with canonical records | `…dashboard_workspace_lists_owned_account_with_last_sync_time` | FIXED | Live balance on the dashboard comes from in-memory `/extension/status`, not from `account_snapshots` (see 9) |
| 11 | Disconnect/reconnect recovers safely | `…disconnect_then_reconnect_recovers_without_losing_history` | PASS | none |
| 12 | Telegram events match real changes | `…closed_trade_alert_is_sent_once…` (FIXED); `…no_telegram_for_rejected_writes` (PASS) | FIXED / **GAP** | The "Trade Closed" alert comes from the extension's in-browser detection (P&L = balance delta). It is not checked against a persisted trade. Risk-level alerts still compare against in-memory state, so they can repeat after a restart |
| — | Real FundingPips account, live page, real extension build | none | **NOT TESTED** | Needs an authorized FundingPips test account and a loaded extension build |
| — | Telegram delivery to a real chat | none | **NOT TESTED** | Needs a bot token for a test bot |

Other launch checks (security, database TLS, readiness, execution gate) are covered by PRs #143–#147 and their tests.
