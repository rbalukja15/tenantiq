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

## 3. Create a client

In the realm, create a client (e.g. `tenantiq-acme`):
- Client authentication: **On** (confidential) for a backend-to-backend flow, or Off for a
  public SPA client used by the M4 frontend.
- Valid redirect URIs / web origins: your frontend URL (`NEXT_PUBLIC_API_URL` host).

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
