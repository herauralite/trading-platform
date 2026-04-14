# Security Remediation Notes

## What was fixed
- Removed tracked runtime secret file(s) and replaced them with safe environment templates.
- Hardened Telegram auth to fail closed when required auth configuration is missing.
- Removed insecure default JWT secret behavior.
- Added secret/session ignore patterns to prevent recommit of local auth artifacts.
- Added a lightweight repository secret scan script (`scripts/secret_scan.sh`).

## Manual rotation required immediately
Treat all previously committed secrets as compromised and rotate:
- Telegram bot token(s)
- Telegram OIDC client secret(s)
- JWT signing secret(s)
- Database credentials/connection strings
- Redis credentials/URLs (if used)
- Cloudflare API tokens (if used)
- MT5 credentials (login/password/server) (if used)
- Any API keys in local/operator `.env` or shell history

## Files that must never be committed
- `.env`, `.env.*`, `config.env`, `config.env.*`
- Session/auth artifacts such as `*.session`, `tdata/`, token caches, auth dumps
- Local desktop Telegram auth exports or client data folders

## Git history warning
Removing secrets from current files is not sufficient.
If secrets were ever committed, history may still expose them.
Perform history rewrite (for example with `git filter-repo` or BFG), force-push rewritten refs, then rotate secrets.

## Remaining risk notes
- **Bot-token exposure** allows API operations as the bot identity.
- **Telegram desktop/session artifact exposure** can be more severe because it may grant direct account session access, not just bot API access.
- Validate host hardening and endpoint logs for misuse during the exposure window.
