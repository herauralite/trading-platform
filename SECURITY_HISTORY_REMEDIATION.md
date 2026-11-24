# Committed-credential remediation plan

Status as of 2026-11-23: **OPEN. Credentials must be treated as compromised.**

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

Owner: repository owner. Claude is not authorized to rotate production credentials.

1. **Neon database**: reset the role password (or create a new role and drop the old one), update
   `DATABASE_URL` on every Railway service, then check `/health/ready`. In Neon, review the connection and
   query history for unknown clients.
2. **Telegram bot**: in @BotFather, run `/revoke` for the bot token and update `TELEGRAM_BOT_TOKEN` on
   Railway. The webhook secret is derived from the token, and the app calls `setWebhook` again on startup.
3. **`SECRET_KEY`**: confirm that production does not use the committed value. If it does (or if you are
   unsure), rotate it. Rotation has side effects:
   * every session token becomes invalid, so all users must log in again;
   * `app/services/secret_crypto.py` derives its encryption key from `SECRET_KEY`. Any connector
     credentials encrypted under the old key become unreadable. Plan to re-encrypt them (decrypt with the
     old key, encrypt with the new one, in one maintenance step), or to have affected users reconnect.
     Match-Trader tokens use their own versioned key and are not affected.
4. Record the evidence for each rotation: the date, who did it, and confirmation that the old credential
   now fails. Do not put values in the record.

## 2. History rewrite (optional hardening, after rotation)

The rewrite only shortens how long the data stays exposed. Clones, forks, caches, and the GitHub PR refs
may already have copies.

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
* Recommended, owner action: turn on GitHub secret scanning and push protection
  (Settings → Code security).

## Remaining exposure until the items above are done

Until the credentials are rotated, anyone who read the public history can connect to the production
database and control the Telegram bot.
