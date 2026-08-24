#!/usr/bin/env bash
#
# Provision the local Keycloak realm a sign-in needs, idempotently (#86).
#
# docs/auth-keycloak.md describes this as five manual steps in an admin console. That is fine to read
# and impossible to automate, so nothing has ever exercised the authenticated path end to end — which
# is exactly how #79, #81 and #84 all shipped green. This does the same five steps through the admin
# REST API and is safe to re-run: every object is created only if absent.
#
#   make dev-realm            # after `make dev` is up
#   TENANT=globex ./scripts/dev-realm.sh
#
# DEV FIXTURES ONLY. The user and password below are test fixtures for a throwaway local IdP, created
# by this script and meaningful nowhere else. Never point this at an IdP that matters.

set -euEo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=".env"

env_get() { # key default — read one value from .env without sourcing the whole file
  local value=""
  [ -f "$ENV_FILE" ] && value="$(sed -n "s/^${1}=//p" "$ENV_FILE" | tail -1)"
  printf '%s' "${value:-$2}"
}

KEYCLOAK_URL="${KEYCLOAK_URL:-http://keycloak:8080}"
KC_ADMIN="${KEYCLOAK_ADMIN:-$(env_get KEYCLOAK_ADMIN admin)}"
KC_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-$(env_get KEYCLOAK_ADMIN_PASSWORD admin)}"
APP_BASE_URL="${APP_BASE_URL:-$(env_get APP_BASE_URL http://localhost:3000)}"

TENANT="${TENANT:-acme}"
TENANT_NAME="${TENANT_NAME:-Acme Inc}"
CLIENT_ID="${CLIENT_ID:-tenantiq-$TENANT}"
SMOKE_USER="${SMOKE_USER:-alice}"
SMOKE_PASSWORD="${SMOKE_PASSWORD:-alice}"
SMOKE_FIRST_NAME="${SMOKE_FIRST_NAME:-Alice}"
SMOKE_LAST_NAME="${SMOKE_LAST_NAME:-Anderson}"

STEP="starting up"
step() { STEP="$1"; printf '  · %s\n' "$1"; }
fail() { printf '\n\033[31mdev-realm failed at: %s\033[0m\n  %s\n' "$STEP" "$1" >&2; exit 1; }
trap 'fail "command failed (see output above)"' ERR

json_get() { # <json-on-stdin> path.expression — "" when absent, never an exception
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for key in sys.argv[1].split("."):
    if isinstance(data, list):
        if not data:
            sys.exit(0)
        data = data[0]
    if not isinstance(data, dict) or key not in data:
        sys.exit(0)
    data = data[key]
print(data if not isinstance(data, (dict, list)) else json.dumps(data))
' "$1"
}

api() { # METHOD path [json-body] — admin REST call, prints the body, fails loudly on >=400
  local method="$1" path="$2" body="${3:-}" out status
  out="$(mktemp)"
  if [ -n "$body" ]; then
    status="$(curl -sS -o "$out" -w '%{http_code}' -X "$method" \
      -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
      --data "$body" "$KEYCLOAK_URL$path")"
  else
    status="$(curl -sS -o "$out" -w '%{http_code}' -X "$method" \
      -H "Authorization: Bearer $ADMIN_TOKEN" "$KEYCLOAK_URL$path")"
  fi
  # 409 is "already there", which for an idempotent provisioner is success, not an error.
  if [ "$status" -ge 400 ] && [ "$status" != "409" ]; then
    fail "$method $path returned $status: $(head -c 400 "$out")"
  fi
  cat "$out"; rm -f "$out"
}

# --- reachability ------------------------------------------------------------------------------
# Checked first and named precisely, because the failure mode is a documented /etc/hosts line that
# everything else silently depends on.
step "reaching Keycloak at $KEYCLOAK_URL"
curl -sS -o /dev/null --max-time 10 "$KEYCLOAK_URL/realms/master/.well-known/openid-configuration" \
  || fail "cannot reach $KEYCLOAK_URL. Two things it is almost always one of:
       1. Keycloak is not running. It sits behind a compose profile, so \`make dev\` alone does not
          start it:  docker compose --profile dev up -d keycloak
       2. Your machine cannot resolve the name. The issuer must be one string that means the same
          Keycloak from the browser, the Next server and Django (docs/auth-keycloak.md §1):
              echo \"127.0.0.1 keycloak\" | sudo tee -a /etc/hosts"

step "authenticating to the admin API as '$KC_ADMIN'"
ADMIN_TOKEN="$(curl -sS -X POST "$KEYCLOAK_URL/realms/master/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=admin-cli \
  --data-urlencode "username=$KC_ADMIN" --data-urlencode "password=$KC_ADMIN_PASSWORD" \
  | json_get access_token)"
[ -n "$ADMIN_TOKEN" ] || fail "admin login failed — check KEYCLOAK_ADMIN / KEYCLOAK_ADMIN_PASSWORD in .env"

# --- realm -------------------------------------------------------------------------------------
step "realm '$TENANT'"
if [ -z "$(api GET "/admin/realms/$TENANT" | json_get realm)" ]; then
  api POST /admin/realms "{\"realm\":\"$TENANT\",\"enabled\":true}" >/dev/null
fi

# --- client ------------------------------------------------------------------------------------
# Public + PKCE, no secret: `Tenant` has no field to store one in (ADR-0013 §1), and turning client
# authentication on makes the token exchange fail with `invalid_client`.
step "client '$CLIENT_ID' (public, PKCE S256, redirect $APP_BASE_URL/api/auth/callback)"
CLIENT_UUID="$(api GET "/admin/realms/$TENANT/clients?clientId=$CLIENT_ID" | json_get id)"
if [ -z "$CLIENT_UUID" ]; then
  api POST "/admin/realms/$TENANT/clients" "$(cat <<JSON
{
  "clientId": "$CLIENT_ID",
  "enabled": true,
  "publicClient": true,
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": false,
  "redirectUris": ["$APP_BASE_URL/api/auth/callback"],
  "attributes": {
    "post.logout.redirect.uris": "$APP_BASE_URL/login",
    "pkce.code.challenge.method": "S256"
  }
}
JSON
)" >/dev/null
  CLIENT_UUID="$(api GET "/admin/realms/$TENANT/clients?clientId=$CLIENT_ID" | json_get id)"
fi
[ -n "$CLIENT_UUID" ] || fail "client '$CLIENT_ID' could not be created or found"

# --- audience mapper ---------------------------------------------------------------------------
# Without this Keycloak puts `account` in `aud` and the client id only in `azp`; the backend requires
# the client id in `aud` and rejects the token. We never disable audience checking (ADR-0002).
step "audience mapper putting '$CLIENT_ID' in aud"
MAPPERS="$(api GET "/admin/realms/$TENANT/clients/$CLIENT_UUID/protocol-mappers/models")"
if ! printf '%s' "$MAPPERS" | grep -q '"tenantiq-audience"'; then
  api POST "/admin/realms/$TENANT/clients/$CLIENT_UUID/protocol-mappers/models" "$(cat <<JSON
{
  "name": "tenantiq-audience",
  "protocol": "openid-connect",
  "protocolMapper": "oidc-audience-mapper",
  "config": {
    "included.client.audience": "$CLIENT_ID",
    "id.token.claim": "false",
    "access.token.claim": "true"
  }
}
JSON
)" >/dev/null
fi

# --- user --------------------------------------------------------------------------------------
# firstName/lastName give the token a `name` claim, so #84's display_name has something readable to
# resolve to even if `preferred_username` is ever dropped from DISPLAY_NAME_CLAIMS.
step "user '$SMOKE_USER' (dev fixture)"
USER_UUID="$(api GET "/admin/realms/$TENANT/users?username=$SMOKE_USER&exact=true" | json_get id)"
if [ -z "$USER_UUID" ]; then
  api POST "/admin/realms/$TENANT/users" "$(cat <<JSON
{
  "username": "$SMOKE_USER",
  "enabled": true,
  "emailVerified": true,
  "email": "$SMOKE_USER@example.invalid",
  "firstName": "$SMOKE_FIRST_NAME",
  "lastName": "$SMOKE_LAST_NAME"
}
JSON
)" >/dev/null
  USER_UUID="$(api GET "/admin/realms/$TENANT/users?username=$SMOKE_USER&exact=true" | json_get id)"
fi
[ -n "$USER_UUID" ] || fail "user '$SMOKE_USER' could not be created or found"

# Always reset: a half-provisioned user from an interrupted run would otherwise fail at the login
# form with an error the flow cannot distinguish from a real regression.
api PUT "/admin/realms/$TENANT/users/$USER_UUID/reset-password" \
  "{\"type\":\"password\",\"value\":\"$SMOKE_PASSWORD\",\"temporary\":false}" >/dev/null

# --- the Tenant row ----------------------------------------------------------------------------
# The issuer is authoritative in the database (ADR-0002); the realm existing in Keycloak means
# nothing until a Tenant row points at it.
step "Tenant row '$TENANT' -> $KEYCLOAK_URL/realms/$TENANT"
docker compose exec -T backend python manage.py shell <<PY >/dev/null || fail "could not write the Tenant row — is the backend service up?"
from app.models import Tenant
Tenant.objects.update_or_create(
    slug="$TENANT",
    defaults={
        "name": "$TENANT_NAME",
        "oidc_issuer": "$KEYCLOAK_URL/realms/$TENANT",
        "oidc_client_id": "$CLIENT_ID",
        "is_active": True,
    },
)
PY

printf '\n\033[32mrealm ready\033[0m — tenant \033[1m%s\033[0m, sign in at %s as %s / %s\n' \
  "$TENANT" "$APP_BASE_URL/login" "$SMOKE_USER" "$SMOKE_PASSWORD"
