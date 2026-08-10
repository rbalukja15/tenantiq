# Local Keycloak setup (dev)

TenantIQ's backend is an OAuth2 **resource server**: it validates Bearer access tokens issued
by a per-tenant Keycloak realm. One **realm = one tenant**, identified by its **issuer URL**
(`Tenant.oidc_issuer`). Tokens are verified against the realm's JWKS; the tenant is resolved
from the verified `iss` claim. See [ADR-0002](adr/0002-tenant-isolation.md).

> Tests don't need any of this — token verification is injectable and CI signs tokens with a
> local test key (no live Keycloak). This is only for running the real flow locally.

## 1. Start Keycloak

```bash
docker compose --profile dev up keycloak   # admin console at http://localhost:8080
```

Log in with `KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD` (see `.env.example`).

## 2. Create a realm (one per tenant)

Create a realm named e.g. `acme`. Its issuer is then
`http://localhost:8080/realms/acme` — this is the value you store in `Tenant.oidc_issuer`.

The frontend fetches `${oidc_issuer}/.well-known/openid-configuration` and **rejects the document
unless its `issuer` matches exactly**, so the stored value has to be the issuer Keycloak actually
advertises. `start-dev` derives it from the request host, which is the usual cause of a mismatch when
the browser reaches Keycloak on one hostname and the Next server reaches it on another. In production
the issuer must be `https`; the `http://localhost:8080/...` value above is dev-only.

## 3. Create a client

In the realm, create a client (e.g. `tenantiq-acme`):
- Client authentication: **Off (public)**. The M4 frontend signs in with Authorization Code +
  PKCE (S256) and sends **no client secret** — turning this On makes the token exchange fail with
  `invalid_client`, and `Tenant` has no field to store a secret in (ADR-0013 §1).
- Valid redirect URIs: exactly `${APP_BASE_URL}/api/auth/callback` — for local dev,
  `http://localhost:3000/api/auth/callback`. Use the exact URI, not a wildcard.
- Valid post-logout redirect URIs: `${APP_BASE_URL}/login`. (Keycloak's `+` shorthand inherits the
  redirect URIs above; set it explicitly if logout bounces to an error page.)
- Web origins: not needed. Under the BFF the browser only ever talks to the Next server, so no
  cross-origin call reaches Keycloak from the page.

Store the client id in `Tenant.oidc_client_id`.

## 4. Add an audience mapper (important)

By default Keycloak puts `account` in the access token's `aud` and the client id in `azp`.
TenantIQ **requires** the client id to be in `aud`. Add a **Client scope → Mappers →
Audience** mapper that includes `tenantiq-acme` in `aud`, and ensure the client uses that scope.
Without this, valid tokens are rejected (by design — we never disable audience checking).

## 5. Register the tenant

Create a matching `Tenant` row (issuer + client id) via the Django shell or admin:

```python
Tenant.objects.create(
    slug="acme", name="Acme Inc",
    oidc_issuer="http://localhost:8080/realms/acme",
    oidc_client_id="tenantiq-acme",
)
```

A token from this realm now resolves to the `acme` tenant; expired / wrong-issuer /
wrong-audience tokens are rejected with 401.
