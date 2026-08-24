#!/usr/bin/env bash
#
# Drive a real sign-in end to end against the running stack (#86).
#
# Four bugs have now shared one shape: the code was correct, the suite was green, and the running
# system did not work. #79 made sign-in impossible; #81 redirected to http://0.0.0.0:3000 after a
# successful login; #84 greeted a person with their `<sub>.<issuer-hash>` identity key. Three of the
# four were found by a human opening the app. The suite could not see any of them, because token
# verification is injectable and every test signs its own token — so nothing ever fetched a real
# discovery document, ran against a server bound to a different host than the browser uses, or read a
# claim it had not itself just written.
#
# This performs a complete OIDC authorization-code flow with PKCE against the local Keycloak, then
# exercises the authenticated product path through the BFF. It asserts the specific things that
# broke, not merely that responses are 200.
#
#   make dev          # in another terminal
#   make smoke-auth
#
# Deliberately not a browser-driving test: every bug above was HTTP-level — redirect targets,
# headers, cookie scope, response bodies — and a headless browser would add flake and weight without
# covering anything extra. Revisit if a bug turns out to be genuinely browser-only.
#
# DEV FIXTURES ONLY. The credentials posted below belong to a throwaway local IdP that
# scripts/dev-realm.sh creates. They authenticate nothing anywhere else.

set -euEo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=".env"

env_get() {
  local value=""
  [ -f "$ENV_FILE" ] && value="$(sed -n "s/^${1}=//p" "$ENV_FILE" | tail -1)"
  printf '%s' "${value:-$2}"
}

APP="${APP_BASE_URL:-$(env_get APP_BASE_URL http://localhost:3000)}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://keycloak:8080}"
TENANT="${TENANT:-acme}"
CLIENT_ID="${CLIENT_ID:-tenantiq-$TENANT}"
SMOKE_USER="${SMOKE_USER:-alice}"
SMOKE_PASSWORD="${SMOKE_PASSWORD:-alice}"
READY_TIMEOUT="${READY_TIMEOUT:-180}"
QUERY_TIMEOUT="${QUERY_TIMEOUT:-180}"

WORK="$(mktemp -d)"
JAR="$WORK/cookies.txt"
BODY="$WORK/body"
HEAD="$WORK/head"
# Cleanup runs on EVERY exit path, not just the happy one. A failed run that leaves its document
# behind poisons the next run: the tenant accumulates near-identical documents, and the citation
# assertion below starts resolving to a previous run's upload. That happened on the first draft.
cleanup() {
  local code=$?
  trap - ERR
  if [ -n "${DOC_ID:-}" ] && [ "${DOC_DELETED:-0}" != "1" ]; then
    curl -sS -o /dev/null --max-time 20 -b "$JAR" -X DELETE "$APP/api/documents/$DOC_ID" \
      -H "Origin: $APP" -H "x-csrf-token: ${CSRF:-}" >/dev/null 2>&1 \
      && printf '  · cleaned up document #%s\n' "$DOC_ID" || true
  fi
  rm -rf "$WORK"
  exit "$code"
}
trap cleanup EXIT

STEP="starting up"
step() { STEP="$1"; printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() {
  printf '\n\033[31m✗ smoke-auth FAILED\033[0m\n  step:   %s\n  reason: %s\n' "$STEP" "$1" >&2
  exit 1
}
trap 'fail "command exited non-zero (see output above)"' ERR

assert_eq()       { [ "$1" = "$2" ] || fail "$3
          expected: $2
          actual:   $1"; }
assert_contains() { case "$1" in *"$2"*) ;; *) fail "$3
          looking for: $2
          in:          $1";; esac; }
assert_ne()       { [ "$1" != "$2" ] || fail "$3 (both are '$1')"; }

http() { # METHOD URL [curl args...] -> prints status code; body in $BODY, headers in $HEAD
  local method="$1" url="$2"; shift 2
  curl -sS --max-time 60 -o "$BODY" -D "$HEAD" -w '%{http_code}' \
    -X "$method" -b "$JAR" -c "$JAR" "$url" "$@"
}

location() { # the Location header of the last response, CR stripped
  tr -d '\r' < "$HEAD" | sed -n 's/^[Ll]ocation: //p' | tail -1
}

cookie() { # the value of a cookie in the jar, "" when absent
  awk -v name="$1" '$6 == name { print $7 }' "$JAR" | tail -1
}

query_param() { # url name -> the decoded value of one query parameter
  python3 -c '
import sys, urllib.parse as u
q = u.parse_qs(u.urlparse(sys.argv[1]).query)
print(q.get(sys.argv[2], [""])[0])
' "$1" "$2"
}

json_field() { # file path.expression
  python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for key in sys.argv[2].split("."):
    if isinstance(data, list):
        if not data:
            sys.exit(0)
        data = data[0]
    if not isinstance(data, dict) or key not in data:
        sys.exit(0)
    data = data[key]
print(data if not isinstance(data, (dict, list)) else json.dumps(data))
' "$1" "$2"
}

printf '\033[1mTenantIQ auth smoke test\033[0m  app=%s  idp=%s  tenant=%s\n' "$APP" "$KEYCLOAK_URL" "$TENANT"

# ================================================================================================
step "0. the stack is up and the realm exists"
# ================================================================================================
curl -sS -o /dev/null --max-time 10 "$APP/login" \
  || fail "cannot reach the app at $APP — is \`make dev\` running?"
./scripts/dev-realm.sh >/dev/null || fail "realm provisioning failed — run ./scripts/dev-realm.sh directly to see why"
ok "app reachable, realm '$TENANT' provisioned"

# ================================================================================================
step "1. POST /api/auth/login starts a real authorization-code flow"
# ================================================================================================
status="$(http POST "$APP/api/auth/login" \
  -H "Origin: $APP" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data "tenant=$TENANT")"
assert_eq "$status" "303" "login should redirect to the IdP"

AUTHORIZE="$(location)"
[ -n "$AUTHORIZE" ] || fail "login returned no Location header"

# The authorize URL must belong to the *IdP*, not to us. A discovery document that silently fell back
# to our own origin would still produce a redirect and still look like a working login (#79).
assert_contains "$AUTHORIZE" "$KEYCLOAK_URL/realms/$TENANT/protocol/openid-connect/auth" \
  "the authorize endpoint is not the configured IdP's"
assert_eq "$(query_param "$AUTHORIZE" response_type)" "code" "response_type must be 'code'"
assert_eq "$(query_param "$AUTHORIZE" client_id)" "$CLIENT_ID" "wrong client_id in the authorize URL"
assert_eq "$(query_param "$AUTHORIZE" redirect_uri)" "$APP/api/auth/callback" \
  "redirect_uri must be the app's own callback"
# PKCE is not optional here: the client is public and holds no secret, so the verifier is the only
# thing binding the code to the session that requested it.
assert_eq "$(query_param "$AUTHORIZE" code_challenge_method)" "S256" \
  "PKCE must be S256 — 'plain' would defeat the point"
challenge="$(query_param "$AUTHORIZE" code_challenge)"
[ "${#challenge}" -ge 43 ] || fail "code_challenge looks too short to be a real S256 digest: '$challenge'"
[ -n "$(cookie tiq_tx)" ] || fail "no transaction cookie was set, so the callback cannot verify state"
ok "authorize URL is the IdP's, with PKCE S256 and our own redirect_uri"

# ================================================================================================
step "2. the IdP serves its own login form"
# ================================================================================================
status="$(http GET "$AUTHORIZE")"
assert_eq "$status" "200" "the IdP did not serve a login page"
FORM_ACTION="$(python3 -c '
import html, re, sys
page = open(sys.argv[1], encoding="utf-8", errors="replace").read()
match = re.search(r"action=\"([^\"]*login-actions/authenticate[^\"]*)\"", page)
print(html.unescape(match.group(1)) if match else "")
' "$BODY")"
[ -n "$FORM_ACTION" ] || fail "no Keycloak login form found on the authorize page — the realm may be misconfigured"
ok "login form served by $KEYCLOAK_URL"

# ================================================================================================
step "3. submitting the fixture credentials returns a code to OUR origin"
# ================================================================================================
status="$(http POST "$FORM_ACTION" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "username=$SMOKE_USER" \
  --data-urlencode "password=$SMOKE_PASSWORD")"
[ "$status" = "302" ] || fail "expected a redirect carrying the authorization code, got $status.
          The credentials may be wrong, or the realm was reprovisioned mid-flow."

CALLBACK="$(location)"
# #81's sibling: the IdP must send the browser back to the app's own origin, which is what
# redirect_uri asked for. A mismatch here means the client registration and APP_BASE_URL disagree.
assert_contains "$CALLBACK" "$APP/api/auth/callback" "the IdP redirected somewhere other than our callback"
[ -n "$(query_param "$CALLBACK" code)" ] || fail "no authorization code in the callback URL"
ok "authorization code returned to $APP/api/auth/callback"

# ================================================================================================
step "4. the callback lands on the app's OWN origin (this is #81)"
# ================================================================================================
status="$(http GET "$CALLBACK")"
assert_eq "$status" "303" "the callback did not redirect"

AFTER_LOGIN="$(location)"
# The regression this exists for. Under compose the Next server is bound with -H 0.0.0.0, so
# `new URL("/", request.url)` resolves to http://0.0.0.0:3000/ — a *different origin*. The session
# cookie is host-scoped, so it is not sent on the follow-up request and a sign-in that just succeeded
# lands back on the login form. Exact equality, because "close enough" is precisely the bug.
assert_eq "$AFTER_LOGIN" "$APP/" \
  "after sign-in the browser must be sent to the app's own origin (#81)"
[ -n "$(cookie tiq_session)" ] || fail "no session cookie was set by the callback"
CSRF="$(cookie tiq_csrf)"
[ -n "$CSRF" ] || fail "no CSRF cookie was set by the callback"
ok "landed on $AFTER_LOGIN with session and CSRF cookies"

# ================================================================================================
step "5. GET /api/me returns a readable name, not an identity key (this is #84)"
# ================================================================================================
status="$(http GET "$APP/api/me")"
assert_eq "$status" "200" "the session did not authenticate against the API"

USERNAME="$(json_field "$BODY" username)"
DISPLAY_NAME="$(json_field "$BODY" display_name)"
assert_eq "$(json_field "$BODY" tenant.slug)" "$TENANT" "authenticated as the wrong tenant"
[ -n "$DISPLAY_NAME" ] || fail "display_name is empty — the shell has nothing readable to greet with (#84)"
# The two are different things and the distinction is the whole fix: `username` is the synthesized
# <sub>.<issuer-hash> key, `display_name` is what a person is called. Reverting #84 removes the
# latter, and this fails on the empty check above.
assert_ne "$DISPLAY_NAME" "$USERNAME" "display_name is the identity key, not a name (#84)"
case "$DISPLAY_NAME" in
  *.*[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) fail "display_name '$DISPLAY_NAME' still looks like an identity key (#84)" ;;
esac
ok "signed in as '$DISPLAY_NAME' (identity key: ${USERNAME:0:16}…)"

# ================================================================================================
step "6. an upload without the CSRF header is refused"
# ================================================================================================
SAMPLE="$WORK/smoke-auth.txt"
cat > "$SAMPLE" <<'DOC'
TenantIQ authenticated smoke test document.

The Zarnix retainer fee is four thousand euros per quarter, invoiced in advance.
Undisputed invoices are payable within forty-five days of receipt.
Either party may terminate this agreement with ninety days written notice.
DOC

status="$(http POST "$APP/api/documents" -H "Origin: $APP" -F "file=@$SAMPLE")"
assert_eq "$status" "403" "an upload with no x-csrf-token must be refused"
ok "403 without x-csrf-token, as designed"

# ================================================================================================
step "7. an authenticated upload reaches READY through the real worker"
# ================================================================================================
status="$(http POST "$APP/api/documents" \
  -H "Origin: $APP" -H "x-csrf-token: $CSRF" -F "file=@$SAMPLE")"
[ "$status" = "201" ] || fail "upload returned $status: $(head -c 300 "$BODY")"
DOC_ID="$(json_field "$BODY" id)"
[ -n "$DOC_ID" ] || fail "upload response carried no document id"
ok "uploaded document #$DOC_ID"

deadline=$(( $(date +%s) + READY_TIMEOUT ))
doc_status=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  http GET "$APP/api/documents/$DOC_ID" >/dev/null
  doc_status="$(json_field "$BODY" status)"
  case "$doc_status" in
    ready)  break ;;
    failed) fail "ingestion failed: $(json_field "$BODY" error)" ;;
  esac
  sleep 3
done
assert_eq "$doc_status" "ready" "document #$DOC_ID did not reach 'ready' within ${READY_TIMEOUT}s"
ok "document #$DOC_ID is ready"

# ================================================================================================
step "8. the document is citable by POST /api/query"
# ================================================================================================
curl -sS --max-time "$QUERY_TIMEOUT" -o "$BODY" -b "$JAR" -c "$JAR" \
  -X POST "$APP/api/query" \
  -H "Origin: $APP" -H "x-csrf-token: $CSRF" -H "Content-Type: application/json" \
  --data '{"question":"What is the Zarnix retainer fee and when are invoices payable?"}' \
  || fail "the query request failed or timed out after ${QUERY_TIMEOUT}s"

assert_contains "$(cat "$BODY")" "event: citations" "the stream never sent its terminal citations frame"

# Parsed into a file rather than through $(...): a command substitution that dies takes its exit
# status with it, so an empty result would be misread as "the query refused" — a different and much
# more alarming finding than "the parser broke".
PARSED="$WORK/citations.txt"
python3 - "$BODY" > "$PARSED" <<'PYEOF' || fail "could not parse the SSE stream (traceback above)"
import json, sys

refused, chunks = None, []
stream = open(sys.argv[1], encoding="utf-8", errors="replace").read()
for block in stream.split("\n\n"):
    if "event: citations" not in block:
        continue
    for line in block.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            refused = bool(payload.get("refused"))
            chunks = [str(c["chunk_id"]) for c in payload.get("citations", [])]
if refused is None:
    sys.exit("no parseable citations frame in the stream")
print(f"{str(refused).lower()}|{','.join(chunks)}")
PYEOF

IFS='|' read -r REFUSED CITED_CHUNKS < "$PARSED"
case "$REFUSED" in
  true|false) ;;
  *) fail "could not read the citations frame's 'refused' flag (got '$REFUSED')" ;;
esac

[ "$REFUSED" = "false" ] || fail "the query refused — retrieval found no evidence in a tenant that
          contains exactly one ready document. Check TENANTIQ_RETRIEVAL_MIN_SIMILARITY."
[ -n "$CITED_CHUNKS" ] || fail "the answer cited nothing, so no citation could be resolved.
          Retrieval succeeded (refused=false), so this is the generator emitting no [n] markers."

# Two separate claims, deliberately not merged.
#
# EVERY citation must resolve to a real chunk. "Citations resolve to real chunk IDs" is a claim this
# project makes in its README, and this is the only place it is checked against a running system
# rather than a fixture.
#
# AT LEAST ONE must belong to the document we just uploaded. Not *all* of them: this runs against a
# real signed-in tenant, which legitimately contains whatever else that tenant has uploaded, and an
# answer drawing on two documents is correct behaviour rather than a failure. The first draft
# asserted "all" and went red the moment the tenant held a second document.
cited_ours=0
for chunk_id in $(printf '%s' "$CITED_CHUNKS" | tr ',' ' '); do
  status="$(http GET "$APP/api/chunks/$chunk_id")"
  assert_eq "$status" "200" "cited chunk $chunk_id does not resolve — the answer invented a citation"
  [ "$(json_field "$BODY" document.id)" = "$DOC_ID" ] && cited_ours=$((cited_ours + 1))
done
[ "$cited_ours" -gt 0 ] || fail "the answer cited $CITED_CHUNKS, none of which belong to the document
          this run uploaded (#$DOC_ID). Retrieval reached the tenant but not this upload."
ok "every cited chunk resolves, $cited_ours of them from document #$DOC_ID"

# ================================================================================================
step "9. the document can be deleted"
# ================================================================================================
status="$(http DELETE "$APP/api/documents/$DOC_ID" -H "Origin: $APP" -H "x-csrf-token: $CSRF")"
[ "$status" = "204" ] || fail "delete returned $status: $(head -c 300 "$BODY")"
DOC_DELETED=1
status="$(http GET "$APP/api/documents/$DOC_ID")"
assert_eq "$status" "404" "the document is still readable after being deleted"
ok "document #$DOC_ID deleted and gone"

printf '\n\033[32m✓ smoke-auth passed\033[0m — a real sign-in, upload, grounded answer and delete all worked.\n'
