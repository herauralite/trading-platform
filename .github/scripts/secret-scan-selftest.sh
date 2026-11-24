#!/usr/bin/env bash
# Proves the pinned gitleaks image, this repository's config and the scan flags still catch each kind of
# credential this repository has leaked. Each fixture is a RANDOMLY GENERATED fake value, committed only to a
# throwaway repository under $RUNNER_TEMP, never to this repository. Fails if any fixture goes undetected,
# or if a harmless control commit is flagged.
set -euo pipefail
IMAGE="$1"                      # e.g. zricethezav/gitleaks:v8.21.2
CONFIG="$PWD/.gitleaks.toml"
WORK="${RUNNER_TEMP:-/tmp}/gitleaks-selftest"
rm -rf "$WORK" && mkdir -p "$WORK"

rand() { python3 -c "import secrets,string,sys; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(int(sys.argv[1]))))" "$1"; }
digits() { python3 -c "import secrets,sys; print(''.join(secrets.choice('0123456789') for _ in range(int(sys.argv[1]))))" "$1"; }

declare -A FIXTURES
FIXTURES[telegram_bot_token]="TELEGRAM_BOT_TOKEN=$(digits 10):A$(rand 34)"
FIXTURES[neon_database_url]="DATABASE_URL=postgresql://neondb_owner:npg_$(rand 12)@ep-$(rand 8 | tr 'A-Z' 'a-z')-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
FIXTURES[asyncpg_database_url]="DATABASE_URL=postgresql+asyncpg://app:$(rand 24)@db.internal:5432/app"
FIXTURES[secret_key]="SECRET_KEY=$(rand 48)"
FIXTURES[github_token]="GH_TOKEN=ghp_$(rand 36)"
FIXTURES[control_ci_placeholder]="DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/talitrade_test"

scan() {  # $1 = fixture name; prints gitleaks' exit code
  local dir="$WORK/$1"
  mkdir -p "$dir"
  git -C "$dir" init -q
  printf '%s\n' "${FIXTURES[$1]}" > "$dir/settings.env.txt"
  git -C "$dir" add . && git -C "$dir" -c user.name=ci -c user.email=ci@invalid commit -q -m fixture
  set +e
  docker run --rm -v "$dir:/repo" -v "$CONFIG:/gitleaks.toml:ro" -w /repo "$IMAGE" \
    git /repo --config /gitleaks.toml --redact --no-banner --exit-code 1 --log-opts="--no-merges HEAD" \
    >"$dir/out.log" 2>&1
  local code=$?
  set -e
  echo "$code"
}

failed=0
for name in "${!FIXTURES[@]}"; do
  code=$(scan "$name")
  if [[ "$name" == control_* ]]; then
    if [[ "$code" == 0 ]]; then echo "ok    $name: not flagged (expected)"; else echo "FAIL  $name: flagged (exit $code)"; failed=1; fi
  else
    if [[ "$code" == 1 ]]; then echo "ok    $name: detected"; else echo "FAIL  $name: NOT detected (exit $code)"; failed=1; fi
  fi
done
rm -rf "$WORK"
exit "$failed"
