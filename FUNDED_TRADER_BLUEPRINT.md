# Built to keep traders funded

The dashboard should answer one question before a trader takes another position. How much room is left under my own plan and under my firm's rules? Show both numbers with the account, currency and observation time. A missing rule or stale feed gets an explicit status.

## Start with one account and one session

1. After Telegram sign-in, the trader selects an owned Match-Trader connection. Show the broker, masked login, account currency, current equity and timestamp. Ask them to confirm account size, goal, stage and personal loss budget in account currency. Keep the 100K account's settings independent from the 5K account.
2. For a first-session pilot, label the start and end explicitly: from confirmed equity now until 00:00 UTC. A user who arrives at noon can start a session from noon. Calculating the whole broker day requires an observed reset baseline and the firm's reset timezone. Require confirmation before changing a live plan, and start a new version.
3. Show the difference between starting equity and current equity, floored at zero. Remaining budget is the limit minus that loss, floored at zero. Equity includes open positions. If funding, withdrawals or credits affect the interval, suspend the estimate until cash flows are reconciled. A loss can exceed the limit between samples.
4. For a 750 USD limit, 600 USD used leaves 150 USD. At 80% send one warning. At 100% send one limit alert. If the first observed crossing is already over 100%, send only the higher alert. Include the trader's chosen action, timestamp, masked account and dashboard link. No order execution.

## Integrate after the shared dashboard work

Use a new plan table keyed by verified owner, connection, session and version. Store the confirmed equity baseline, provider timestamp, currency, budget, chosen reminder, expiry and reset convention. Create it in an ownership-checked transaction using the latest fresh stored snapshot. The browser supplies the budget and preferences; the server obtains identity and baseline. Do not read credentials or make extra broker calls for alerts.

Expose create, current and end-plan actions to the authenticated dashboard. The private bot can offer equivalent setup through a command with account selection and a confirmation step. Recheck active ownership on each action, including after disconnect/reconnect. Use the same calculation for the dashboard and bot. The initial calculation is in backend/app/services/funded_plan.py; it currently has no production caller.

Evaluate each fresh snapshot against the active plan. Record each threshold in a transactional outbox unique to owner, connection, session, plan version and threshold. Respect /stop, retry transient delivery failures with backoff, and cancel pending alerts on expiry, opt-out or disconnect. Watch for stale, future-dated or out-of-order observations. Show the last observation time while data is unavailable. Resume only with a valid baseline.

## Add challenge rules and payouts

Capture the firm's dated rule source and trader confirmation. For each phase, store profit target, minimum qualifying trading days, daily loss calculation, reset timezone, overall loss type (static, intraday trailing or end-of-day trailing), reference balance/equity and any cap on the trailing floor. Keep personal limits independent. Unsupported or ambiguous rules stay unknown. Never infer a target from the advertised account size.

For funded accounts, collect first eligible payout date, minimum profit, split, consistency requirements and rules about open positions. Display each criterion as met, unmet or unknown with the evidence and date. After a confirmed phase transition, archive the previous version and establish the next phase's baseline. Preserve trade history.

## Release checks

Pilot with one consenting user and two accounts. Replay a gaining session, the 600/750 example, a jump across both thresholds, overnight reset, DST, stale sync, withdrawal, duplicate snapshot, worker restart, plan edit, opt-out and disconnected account. Verify a second Telegram user's rows and messages remain isolated. Then send a real opted-in test notification and compare the dashboard and phone values. Keep the new evaluator behind its own switch so personal warnings can be paused independently of existing bot alerts.
