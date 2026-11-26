#!/usr/bin/env bash
# Disposable rehearsal of the proposed production step "back up certs/, remove server.crt, restart Postgres"
# against Railway's published postgres-ssl image ($IMAGE). Everything runs in throwaway containers on the CI
# runner. Prints public certificate facts, file ownership/permissions and PASS/FAIL only: no private key
# material, no password, no connection string.
set -uo pipefail

: "${IMAGE:?set IMAGE}"
NET=rehearsal-net
VOL=rehearsal-pgdata
PG=rehearsal-pg
PRIVATE_NAME=postgres.railway.internal          # a test name on the runner's docker network only
CERTS=/var/lib/postgresql/data/certs
PGDATA_DIR=/var/lib/postgresql/data/pgdata      # Railway's template layout: volume at /var/lib/postgresql/data
PW=$(openssl rand -hex 24)
echo "::add-mask::$PW"

step() { echo; echo "=================== $* ==================="; }
fail=0
check() { if eval "$2"; then echo "PASS: $1"; else echo "FAIL: $1"; fail=1; fi; }

pg_run() {   # $1 = extra docker args
  # shellcheck disable=SC2086
  docker run -d --name "$PG" --network "$NET" --network-alias "$PRIVATE_NAME" \
    -v "$VOL:/var/lib/postgresql/data" -e PGDATA="$PGDATA_DIR" \
    -e POSTGRES_USER=rehearsal -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=rehearsal $1 "$IMAGE" >/dev/null
}
wait_ready() {
  for _ in $(seq 1 90); do
    if docker exec "$PG" pg_isready -h 127.0.0.1 -U rehearsal -d rehearsal >/dev/null 2>&1; then
      # pg_isready answers during the initdb temp server; require the final server (TLS on) too.
      if docker exec -e PGPASSWORD="$PW" "$PG" psql "host=127.0.0.1 user=rehearsal dbname=rehearsal sslmode=require" \
           -tAc "select 1" >/dev/null 2>&1; then echo "ready"; return 0; fi
    fi
    sleep 2
  done
  echo "NOT READY"; docker logs --tail 40 "$PG" 2>&1 | sed 's/'"$PW"'/***/g'; return 1
}
cert_facts() {   # public facts + ownership/permissions; key files only as public-key hashes
  docker exec -u root "$PG" bash -c '
    cd '"$CERTS"' || exit 1
    echo "files:"; stat -c "  %n %U:%G %a" * | sort
    for c in root.crt server.crt; do
      echo "$c:"; openssl x509 -in $c -noout -subject -issuer -enddate -fingerprint -sha256 | sed "s/^/  /"
      openssl x509 -in $c -noout -ext subjectAltName,basicConstraints,keyUsage,subjectKeyIdentifier,authorityKeyIdentifier 2>/dev/null | sed "s/^/  /"
    done
    for k in root.key server.key; do printf "  %s public-key sha256=%s\n" $k "$(openssl pkey -in $k -pubout 2>/dev/null | sha256sum | cut -c1-16)"; done'
}
fp() { docker exec -u root "$PG" bash -c "openssl x509 -in $CERTS/$1 -noout -fingerprint -sha256 | cut -d= -f2"; }
keyfp() { docker exec -u root "$PG" bash -c "openssl pkey -in $CERTS/$1 -pubout | sha256sum | cut -c1-16"; }
perms() { docker exec -u root "$PG" bash -c "cd $CERTS && stat -c '%n %U:%G %a' * | sort"; }
app() {   # $1 = mode label; rest = env assignments. Runs the repo's probe or engine in Python 3.13.
  local what=$1; shift
  docker run --rm --network "$NET" -v "$PWD/backend:/app:ro" -v "$PWD/rehearsal-ca:/ca:ro" -w /app \
    -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PYTHONDONTWRITEBYTECODE=1 \
    -e DATABASE_URL="postgresql+asyncpg://rehearsal:$PW@$PRIVATE_NAME:5432/rehearsal" \
    -e RAILWAY_ENVIRONMENT_NAME=production "$@" python:3.13-slim bash -c '
      pip install -q --root-user-action=ignore SQLAlchemy==2.0.46 asyncpg==0.31.0 certifi==2026.1.4 cryptography==46.0.5 python-dotenv >/dev/null 2>&1
      python -c "import sys; print(\"python\", sys.version.split()[0])"
      if [ "'"$what"'" = probe ]; then python -m app.core.db_tls_probe; else python - <<PY
import asyncio
from sqlalchemy import text
from app.core import database, health
print(database.describe_tls(database.ssl_context))
async def main():
    try:
        async with database.engine.connect() as c:
            n = (await c.execute(text("select count(*) from canary"))).scalar()
            tls = (await c.execute(text("select ssl from pg_stat_ssl where pid = pg_backend_pid()"))).scalar()
            print(f"CONNECTED canary_rows={n} server_reports_ssl={tls}")
    except BaseException as exc:
        print("CONNECT_FAILED", type(exc).__name__, health.tls_failure_reason(exc))
asyncio.run(main())
PY
      fi' 2>&1 | sed "s/$PW/***/g"
}

docker pull -q "$IMAGE" >/dev/null
step "image"
docker image inspect "$IMAGE" --format 'digest={{index .RepoDigests 0}} created={{.Created}}'
docker image inspect "$IMAGE" --format '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' | grep -iE 'revision|source|created|version' || true
docker network create "$NET" >/dev/null; docker volume create "$VOL" >/dev/null
mkdir -p rehearsal-ca

step "A. legacy state: volume initialised WITHOUT RAILWAY_PRIVATE_DOMAIN (production's localhost-only cert)"
pg_run ""
wait_ready || exit 1
docker exec -e PGPASSWORD="$PW" "$PG" psql -q "host=127.0.0.1 user=rehearsal dbname=rehearsal" \
  -c "create table canary(id int); insert into canary values (1),(2),(3);"
docker exec "$PG" id
docker logs "$PG" 2>&1 | grep -iE "init-ssl|certificat" | head -5
cert_facts
docker rm -f "$PG" >/dev/null

step "B. same volume, container now has RAILWAY_PRIVATE_DOMAIN (as Railway provides): cert must be KEPT"
pg_run "-e RAILWAY_PRIVATE_DOMAIN=$PRIVATE_NAME"
wait_ready || exit 1
ORIG_ROOT=$(fp root.crt); ORIG_LEAF=$(fp server.crt); ORIG_RK=$(keyfp root.key); ORIG_SK=$(keyfp server.key); ORIG_PERMS=$(perms)
docker logs "$PG" 2>&1 | grep -iE "regenerat|generating cert|init-ssl" | head -5 || true
check "restart with the variable alone does not regenerate (SAN still localhost only)" \
  "! docker exec -u root $PG openssl x509 -in $CERTS/server.crt -noout -ext subjectAltName | grep -q $PRIVATE_NAME"
cp /dev/null rehearsal-ca/root.pem; docker exec -u root "$PG" cat "$CERTS/root.crt" > rehearsal-ca/root.pem
echo "--- probe, legacy cert, CA taken from the DB container ---"
app probe -e TALITRADE_DB_SSL_ROOT_CERT=/ca/root.pem
echo "--- app engine, current production mode (require + owner exception) ---"
app engine -e TALITRADE_DB_SSL_MODE=require -e TALITRADE_DB_TLS_UNVERIFIED_EXCEPTION=railway-private-network

step "C. proposed change: back up certs/, remove server.crt, restart"
docker exec -u root "$PG" bash -c "cp -a $CERTS ${CERTS}.bak-rehearsal && rm $CERTS/server.crt"
docker exec -u root "$PG" bash -c "stat -c '%n %U:%G %a' ${CERTS}.bak-rehearsal ${CERTS}.bak-rehearsal/* | sort"
T0=$(date +%s); docker restart -t 30 "$PG" >/dev/null; wait_ready || exit 1; echo "restart-to-ready seconds=$(( $(date +%s) - T0 ))"
docker logs "$PG" 2>&1 | grep -iE "without certificate|init-ssl|regenerat|generating" | tail -5
cert_facts
NEW_ROOT=$(fp root.crt); NEW_LEAF=$(fp server.crt)
check "leaf certificate replaced" "[ '$NEW_LEAF' != '$ORIG_LEAF' ]"
check "root CA replaced (clients pinning the old CA would break)" "[ '$NEW_ROOT' != '$ORIG_ROOT' ]"
check "server private key replaced" "[ '$(keyfp server.key)' != '$ORIG_SK' ]"
check "root private key replaced" "[ '$(keyfp root.key)' != '$ORIG_RK' ]"
check "SAN now includes the private hostname" \
  "docker exec -u root $PG openssl x509 -in $CERTS/server.crt -noout -ext subjectAltName | grep -q 'DNS:$PRIVATE_NAME'"
check "data survived" "[ \"\$(docker exec -e PGPASSWORD=$PW $PG psql 'host=127.0.0.1 user=rehearsal dbname=rehearsal' -tAc 'select count(*) from canary')\" = 3 ]"
docker exec -u root "$PG" cat "$CERTS/root.crt" > rehearsal-ca/root.pem
echo "--- probe with the CA fetched from the DB container (strict rows first, then x509_strict=off) ---"
app probe -e TALITRADE_DB_SSL_ROOT_CERT=/ca/root.pem
echo "--- app engine verify-full, strict ON ---"
app engine -e TALITRADE_DB_SSL_ROOT_CERT=/ca/root.pem
echo "--- app engine verify-full, x509_strict=off ---"
app engine -e TALITRADE_DB_SSL_ROOT_CERT=/ca/root.pem -e TALITRADE_DB_SSL_X509_STRICT=off
echo "--- app engine, current production mode (require + exception) after regeneration ---"
app engine -e TALITRADE_DB_SSL_MODE=require -e TALITRADE_DB_TLS_UNVERIFIED_EXCEPTION=railway-private-network
echo "--- which strict-mode defect: OpenSSL strict verify of the leaf against the fetched CA ---"
docker exec -u root "$PG" bash -c "openssl version; openssl verify -x509_strict -CAfile $CERTS/root.crt $CERTS/server.crt; openssl verify -CAfile $CERTS/root.crt $CERTS/server.crt" 2>&1 | sed "s#$CERTS/##g"

step "D. rollback: restore the backed-up directory as a unit, restart"
docker exec -u root "$PG" bash -c "rm -rf $CERTS && cp -a ${CERTS}.bak-rehearsal $CERTS"
docker restart -t 30 "$PG" >/dev/null; wait_ready || exit 1
docker logs --since 60s "$PG" 2>&1 | grep -iE "init-ssl|regenerat|generating|keeping" | tail -5 || true
check "rollback: original root CA back" "[ '$(fp root.crt)' = '$ORIG_ROOT' ]"
check "rollback: original leaf back" "[ '$(fp server.crt)' = '$ORIG_LEAF' ]"
check "rollback: original server key back" "[ '$(keyfp server.key)' = '$ORIG_SK' ]"
check "rollback: ownership and permissions identical" "[ \"\$(perms)\" = \"\$ORIG_PERMS\" ]"
check "rollback: data intact" "[ \"\$(docker exec -e PGPASSWORD=$PW $PG psql 'host=127.0.0.1 user=rehearsal dbname=rehearsal' -tAc 'select count(*) from canary')\" = 3 ]"
echo "--- app engine, current production mode, after rollback ---"
app engine -e TALITRADE_DB_SSL_MODE=require -e TALITRADE_DB_TLS_UNVERIFIED_EXCEPTION=railway-private-network

step "E. negative control: restore with the WRONG permission on server.key (what a careless copy does)"
docker exec -u root "$PG" bash -c "chmod 644 $CERTS/server.key"
docker restart -t 30 "$PG" >/dev/null
sleep 15
docker logs --since 30s "$PG" 2>&1 | grep -iE "FATAL|permission|private key" | head -3 || true
docker inspect -f 'container state after bad permission: {{.State.Status}} restarting={{.State.Restarting}} exit={{.State.ExitCode}}' "$PG"
docker stop -t 10 "$PG" >/dev/null 2>&1
docker run --rm -u root -v "$VOL:/var/lib/postgresql/data" --entrypoint chmod "$IMAGE" 600 "$CERTS/server.key"
docker start "$PG" >/dev/null; wait_ready && echo "recovered after fixing the permission"

docker rm -f "$PG" >/dev/null; docker volume rm "$VOL" >/dev/null; docker network rm "$NET" >/dev/null
echo; [ $fail = 0 ] && echo "REHEARSAL: all checks passed" || echo "REHEARSAL: some checks FAILED"
exit $fail
