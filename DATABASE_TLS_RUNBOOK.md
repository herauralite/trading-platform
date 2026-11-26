# Production database TLS: state, renewal and rollback

Owner runbook. Never paste a password, private key, token or full connection string into chat, an issue, a
PR or a log. Certificates (`root.crt`, `server.crt`) are public; `*.key` files are not, so never print them.

## Current state (2026-11-26)

| Item | Value |
|---|---|
| API service | Railway project `scintillating-connection`, service `trading-platform`, environment `production` |
| Database | Railway Postgres (`postgres-ssl:18` image) on `*.railway.internal:5432` (WireGuard private network) |
| App TLS mode | `verify-full` (default; `TALITRADE_DB_SSL_MODE` unset): chain **and** host name verified, TLS 1.3 |
| Trust anchor | `TALITRADE_DB_SSL_ROOT_CERT_PEM` on the API service = Postgres `root.crt` |
| Pinned CA | `CN=root-ca`, SHA-256 prefix `dcff5be67e192af4450e7ff02ac84009`, **expires 2029-02-23 02:34:25Z** |
| Server certificate | `CN=localhost`, SAN `localhost` + the private domain; same expiry; signed by the pinned CA |
| Owner-approved exception | `TALITRADE_DB_SSL_X509_STRICT=off` (2026-11-26). Railway's generated root CA has **no keyUsage** extension, which Python 3.13's `VERIFY_X509_STRICT` rejects (`verify_code=92`). Only that flag is cleared; signature, validity, basicConstraints and host name are still checked. |
| Startup log (proof) | `Database TLS: server identity verified (chain=yes, hostname=yes, trust=pinned CA, x509_strict=off)` and `Database TLS: pinned CA expires in N days`, logged once at the first connection (#159) |
| Rollback backup | `/var/lib/postgresql/data/certs.bak-20261126T022837Z` on the Postgres volume (pre-regeneration certificates). **Keep until the owner closes the rollback window**; delete only with approval. |

History: #144 (verify-full, outage) → #153 revert → #156/#158 probe → certificate regenerated 2026-11-26 →
#154 (verify-full with pinned CA) → #159 (visible log line).

## When renewal is needed

Railway's image (`init-ssl.sh` in [railwayapp-templates/postgres-ssl](https://github.com/railwayapp-templates/postgres-ssl))
regenerates **the CA and the server certificate together**, on a Postgres container start, when `server.crt` is
missing or expires within 30 days. Lifetime is 820 days (`SSL_CERT_DAYS`).

* From **2029-01-24** (30 days before expiry), **any Postgres restart or redeploy replaces the CA**. The API then
  refuses new connections (`tls_verify_code=...` in its startup log) until the new CA is pinned.
* The API logs `pinned CA expires in N days` at WARNING from 60 days before expiry (about **2028-12-25**).
* An unplanned replacement also happens after a pgbackrest restore (certificates are not in the backup) or if
  `server.crt` is deleted.

Plan the renewal before 2029-01-24 and avoid Postgres restarts in the 30-day window until it is done.

## Renewal / rotation procedure (one change at a time, health after each)

0. **Rehearse** on the current image: Actions → `pg-cert-rehearsal` → Run workflow. It runs Railway's
   published image in throwaway containers (no production access). All checks must pass.
1. **Baseline:** `/health/ready` and `/health/db` = 200; note the current CA fingerprint (Postgres console):
   `openssl x509 -in /var/lib/postgresql/data/certs/root.crt -noout -fingerprint -sha256 -enddate`.
2. **Bridge the API** (owner decision; the encrypted-but-unverified exception, time-boxed): on the API service set
   `TALITRADE_DB_SSL_MODE=require` and `TALITRADE_DB_TLS_UNVERIFIED_EXCEPTION=railway-private-network`; deploy;
   health 200; the log shows `server identity NOT verified (... owner exception=railway-private-network ...)`.
   (Skipping this step means an API outage between step 4 and step 7.)
3. **Back up** (Postgres console, as root):
   `cp -a /var/lib/postgresql/data/certs /var/lib/postgresql/data/certs.bak-$(date -u +%Y%m%dT%H%M%SZ)` and check
   `stat -c '%n %U:%G %a'` on the copy (`postgres:postgres`, keys `600`).
4. **Regenerate:** `rm /var/lib/postgresql/data/certs/server.crt`, then Railway → Postgres → **Restart** (not
   Redeploy). Logs show `Database initialized without certificate, generating certificates...`.
5. **Check the new certificate** (Postgres console):
   `openssl x509 -in /var/lib/postgresql/data/certs/server.crt -noout -ext subjectAltName | grep -c "DNS:$RAILWAY_PRIVATE_DOMAIN"`
   must print `1`; `server.key` must stay `postgres:postgres 600`. Health 200, account sync advances.
6. **Verify the replacement CA before rollout.** Read the new CA's fingerprint and public PEM from
   `root.crt` in the Postgres console. In the API console run the probe with the PEM for that one command only:
   ```
   TALITRADE_DB_SSL_ROOT_CERT_PEM="$(cat <<'PEM'
   -----BEGIN CERTIFICATE-----
   ...new root.crt...
   -----END CERTIFICATE-----
   PEM
   )" /app/.venv/bin/python -m app.core.db_tls_probe | grep -v '^cert\[0\]'
   ```
   Required: `cert[1]` sha256 prefix = the new fingerprint; `check configured-ca/verify-full x509_strict=off: PASS`.
   (`configured-ca/verify-full: FAIL verify_code=92` is expected while Railway's CA lacks keyUsage.) Anything else:
   stop, keep the bridge, investigate.
7. **Pin and re-enable verification:** set `TALITRADE_DB_SSL_ROOT_CERT_PEM` to the new PEM, **remove**
   `TALITRADE_DB_SSL_MODE` and `TALITRADE_DB_TLS_UNVERIFIED_EXCEPTION`; deploy. The startup log must show
   `server identity verified (chain=yes, hostname=yes, trust=pinned CA, x509_strict=off)` and the new
   `pinned CA expires in ~819 days`; health 200; `/orders` 403; a linked account syncs.
8. **Record** the date, new fingerprint prefix and expiry in this file. Keep the backup until the rollback
   window is closed.

## Rollback

* **API cannot connect after a CA change:** set `TALITRADE_DB_SSL_MODE=require` +
  `TALITRADE_DB_TLS_UNVERIFIED_EXCEPTION=railway-private-network` (fastest), or redeploy the previous API
  deployment. Then re-pin (steps 5–7).
* **Postgres will not start after regeneration:** Postgres console:
  `rm -rf /var/lib/postgresql/data/certs && cp -a /var/lib/postgresql/data/certs.bak-<stamp> /var/lib/postgresql/data/certs`,
  Restart, confirm the old fingerprint is back. Rehearsed on `postgres-ssl` 16/17/18. If the API was already
  pinned to the new CA, re-pin the old PEM or use the bridge.

## Rehearsal

`scripts/pg_cert_rehearsal.sh` and `.github/workflows/pg-cert-rehearsal.yml` (manual `workflow_dispatch` only)
reproduce steps 3–6 and the rollback against Railway's published image. They use a random per-run password and
print only public certificate facts. First run (2026-11-26, Actions run 45740949548): all checks passed on 16/17/18;
regeneration replaces the CA, both keys and the leaf; SAN gains the private name; data intact; restoring the
backup restores fingerprints and permissions; a `644` `server.key` stops Postgres (exit 1).

## Alternatives, if the strict-X.509 exception must go

* A self-managed fork of `postgres-ssl` with a long-lived CA that has `keyUsage=keyCertSign,cRLSign` (renewals
  then re-sign only the leaf, so no re-pin). Railway notes a changed template "makes the database yours to manage".
* Keeping `require` under the recorded owner exception (encrypted, server identity not verified).
