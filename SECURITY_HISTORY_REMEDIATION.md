# Committed-credential remediation plan

Status as of 2026-11-26: **OPEN.** The production DB password has been rotated. The Telegram token,
`SECRET_KEY` and the historical Neon credential have not; treat them as compromised.

## Verified findings (no values reproduced)

The repository `herauralite/trading-platform` is **public**: the GitHub API answered an unauthenticated
request with HTTP 200. `backend/.env` is still in the published history. A local `git-filter-repo` rewrite
was never pushed, and the clone this was checked from still has the file in its history too.

| Commit (short) | Sensitive keys present (non-empty) |
|---|---|
| `bd81baf` | `DATABASE_URL` (Neon host, with credentials), `SECRET_KEY` |
| `f41b4de`, `a88818d` | `DATABASE_URL` (Neon host), `SECRET_KEY`, `TELEGRAM_BOT_TOKEN` |
| `e1ed682`, `225821b` | `TELEGRAM_BOT_TOKEN` |
| `58cb5e2` | `TELEGRAM_BOT_TOKEN` (its `DATABASE_URL` is a local SQLite path) |
| `185a982`, `4658918` | file emptied or deleted, no values |

All of these commits can be reached from `main`, from most remote branches, and from GitHub's
`refs/pull/*` refs (138 PR refs exist). **No `TELEGRAM_OIDC_CLIENT_SECRET` value was ever committed**
(the key is always empty).

The value committed as `SECRET_KEY` looks like a placeholder. If production ever used it, anyone can forge
session tokens.

## 1. Rotation (required; history removal does not replace it)

Owner: repository owner. Claude is not authorized to rotate production credentials. Never paste a
credential into chat, an issue, a PR or a log. Production API service = Railway `scintillating-connection`.

### Status (2026-11-26, 04:20Z)

How the checks were made (no values anywhere): SHA-256 of each value committed in `backend/.env` history was
computed locally; inside the production API container a one-off script compared the hashes of the live variables
against them and printed only `True`/`False`.

| Credential | State | Evidence (no values) |
|---|---|---|
| Production DB (`DATABASE_URL`, Railway Postgres on `*.railway.internal`) | **Rotated** by the owner | Redeploys 2026-11-26 00:23Z ok, 00:28Z failed (`InvalidPasswordError`), 01:03Z ok; health 200; linked-account sync confirmed by the owner. Live value is not any committed DSN (`LEAKED_DATABASE_URL_IN_USE=False`). |
| Historical Neon `DATABASE_URL` (both values in history) | **Not in use; revocation unconfirmed** | Production does not use it (above) and `diplomatic-manifestation` has no variables at all. Owner: delete the Neon project or role, or reset its password, and record that here. |
| `TELEGRAM_BOT_TOKEN` | **Leaked token revoked** (rotation complete) | Live token differs from the committed one (`LEAKED_TELEGRAM_TOKEN_IN_USE=False`) but belongs to the same bot (`SAME_BOT_ID_AS_LEAKED_TOKEN=True`). Telegram keeps one valid token per bot, so the committed token was revoked. The live token works: webhook registered (HTTP 200) on every deploy, and the live trade alert passed. |
| `SECRET_KEY` | **Committed value not in use; no rotation required for the leak** | `LEAKED_SECRET_KEY_IN_USE=False`; live key is 32 characters, 32 distinct, not a placeholder; `SECRET_KEY_PREVIOUS` unset. Sessions forged with the committed value are rejected, and stored credentials are not encrypted with it (sync works). Rotate only if the *current* value is suspected exposed: steps in 1.2. |

### 1.1 Telegram bot token

The bot is offline from `/revoke` until the new token is deployed, so do steps 1-3 back to back.

1. In @BotFather: `/revoke` → choose the bot → copy the new token. (The old token stops working right away.)
2. Railway → `scintillating-connection` → Variables: replace `TELEGRAM_BOT_TOKEN`, then deploy. Change it
   on any other service that holds the old token as well (see 1.3).
3. Deploy logs must show `Telegram webhook registered successfully (HTTP 200)`. The webhook secret is
   derived from the token, so it changes automatically.
4. Delivery check: send `/start` (or another bot command) to the bot and get a reply. Then trigger
   one trade alert to your own chat (the #152 check) and confirm it arrives.
5. Old token is dead: locally, without echoing it:
   ```powershell
   $t = Read-Host "old token" -AsSecureString; $p = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($t)); try { Invoke-RestMethod "https://api.telegram.org/bot$p/getMe" | Out-Null; "OLD TOKEN STILL WORKS" } catch { "old token rejected: " + $_.Exception.Response.StatusCode.value__ }; Remove-Variable p, t
   ```
   Expected: `old token rejected: 401`.

### 1.2 `SECRET_KEY`

Effects: every session becomes invalid (all users sign in again, by design: sessions are not accepted
under the previous key). Stored connector credentials stay readable through `SECRET_KEY_PREVIOUS` until
they are re-encrypted. Match-Trader tokens use their own AES-GCM key and are not touched.

1. **Announce** a short sign-out window to users.
2. Generate the new key locally and keep it out of shells' history and chat:
   `python -c "import secrets; print(secrets.token_urlsafe(64))"`, then paste it straight into Railway.
3. Railway → `scintillating-connection` → Variables, in one change: `SECRET_KEY_PREVIOUS` = the current
   `SECRET_KEY`; `SECRET_KEY` = the new key. Deploy. Check `/health/ready` = 200.
4. Dry run in the service shell (`railway ssh`): `python -m app.tools.reencrypt_secrets`.
   The output is counts only. **Continue only if the last line shows `undecryptable=0`.** If it is not
   0, stop: those rows were already unreadable before this rotation (or were encrypted with another key),
   and the owner decides whether the affected users reconnect.
5. Apply: `python -m app.tools.reencrypt_secrets --apply`. Run the dry run again: it must show `rotated=0`
   and `undecryptable=0` (everything `already_current`).
6. Sign in again and confirm that a linked account syncs: the sync time advances and balance/equity
   update. Check the deploy logs for decrypt errors.
7. Only then remove `SECRET_KEY_PREVIOUS`, deploy, and confirm account sync once more.
8. Record: date, who did it, dry-run and apply counts, and the sync confirmation.

### 1.3 Other services

`diplomatic-manifestation` (Railway project `176aa183…`) is in a different Railway
   project from production (`38f46222…`). It cannot reference production's variables or reach its
   `*.railway.internal` database. Check its Variables tab (names only) for a copied `DATABASE_URL`,
   `TELEGRAM_BOT_TOKEN` or `SECRET_KEY`. It has never reached a successful deploy (GitHub Deployments).

### 1.4 Record

Record the evidence for each rotation: the date, who did it, and confirmation that the old credential
   now fails. Do not put values in the record.

## 2. History rewrite (optional hardening, after rotation)

The rewrite only shortens how long the data stays exposed. Clones, forks, caches, and the GitHub PR refs
may already have copies. **Do it only after every credential in 1. is rotated, and only with explicit
owner approval: it force-pushes every branch.**

Inventory (2026-11-26 04:20Z, read-only): **all 155 branches, all 148 `refs/pull/*` refs and all 13 other open
PRs** (#145, #141, #138, #136, #123, #98, #83, #35, #29, #14, #12, #5, and this one) contain leaked commits. No tags;
no forks; one collaborator (the owner) plus the automated Codex branches. A full-history
pickaxe scan for the Telegram-token and DSN-with-password patterns matches **only `backend/.env`**
(commits `bd81baf`, `f41b4de`, `e1ed682`, `185a982`, `4658918`), so removing that one path is enough.
`git-filter-repo` is not installed on the owner's machine (`pip install git-filter-repo`).

1. **Backup**: `git clone --mirror https://github.com/herauralite/trading-platform.git backup.git` and
   store it privately. Note that it contains the secrets.
2. **Coordinate**: freeze merges, list open PRs and active branches, and tell collaborators (including the
   automated Codex branches) that every clone must be re-cloned afterwards.
3. **Rewrite on a fresh mirror**: `git filter-repo --invert-paths --path backend/.env`. Then check:
   `git log --all -- backend/.env` must return nothing.
4. **Push** branch by branch with `git push --force-with-lease=<branch>:<expected-old-sha>`, so no
   concurrent update is overwritten. Requires explicit owner approval.
5. **PR refs**: `refs/pull/*` cannot be force-pushed. Ask GitHub Support to purge cached views and the
   unreachable objects for the affected commit SHAs.
6. **Verify**: from a fresh clone, `git log --all -- backend/.env` is empty, and the old commit URLs on
   github.com return 404 (once Support has acted).
7. **After the rewrite**: open PRs have to be rebased or recreated, and old SHAs in issues and PRs stop
   resolving.

## 3. Preventive controls (this PR)

* `.github/workflows/secret-scan.yml`: gitleaks scans the commits each PR or push adds, with findings
  redacted. It also fails if any `.env` file is tracked.
* `.gitignore` ignores `.env.*` (except `.env.example`), `**/.env`, and key-material extensions.
* **GitHub secret scanning and push protection: still disabled** (GitHub API `security_and_analysis`,
  2026-11-26 04:20Z). Tracked separately from the gitleaks CI job: gitleaks only scans commits CI sees, while
  push protection blocks a secret before it reaches GitHub. Owner: Settings → Code security → enable both (free
  on public repositories), then re-check the API.

## Remaining exposure until the items above are done

Until the remaining credentials are rotated, anyone who read the public history can control the Telegram
bot, forge session tokens if production ever used the committed `SECRET_KEY`, and use the Neon database if
that role still exists. (The production Railway database is on a private network and its password was
rotated.)
