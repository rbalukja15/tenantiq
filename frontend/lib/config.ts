/**
 * Server-side configuration for the BFF (#18, ADR-0013 §1).
 *
 * Everything here is exported as a **function**, not a module-scope constant, and validates on first
 * call. That is deliberate: validating at import time would throw while `next build` compiles the
 * route modules — CI runs the build with no environment set — turning a missing variable into an
 * opaque build failure instead of a legible request-time error.
 *
 * None of this may ever be imported by a client component. The values describe how the *server*
 * reaches Django and how it mints cookies; leaking them into the browser bundle would defeat the
 * point of putting a proxy in front of the API.
 */

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is not set. The BFF needs it to reach the API and to mint cookies; see frontend/README.md.`,
    );
  }
  return value.replace(/\/$/, "");
}

function memoize<T>(fn: () => T): () => T {
  let cached: { value: T } | undefined;
  return () => (cached ??= { value: fn() }).value;
}

/**
 * The Django origin, as seen **from the Next server**.
 *
 * Deliberately not `NEXT_PUBLIC_API_URL`: that one is inlined into the browser bundle at build time
 * and its compose value (`http://localhost:8000`) is wrong for a server-side fetch running inside
 * the compose network, where the API is `http://backend:8000`. There is no fallback to it — a silent
 * fallback to a browser-shaped URL is precisely the bug this separation exists to prevent.
 */
export const apiBaseUrl = memoize(() => required("API_BASE_URL"));

/**
 * The app's own public origin. Used for `redirect_uri`, `post_logout_redirect_uri`, and — most
 * importantly — as the value every state-changing request's `Origin` header must equal.
 */
export const appBaseUrl = memoize(() => required("APP_BASE_URL"));

/** Cookies get `Secure` exactly when the app is served over https. */
export const cookieSecure = memoize(() => new URL(appBaseUrl()).protocol === "https:");

/**
 * Development opt-in: permit an OIDC issuer served over plain **http** on a non-loopback host (#79).
 *
 * `docs/auth-keycloak.md` requires the local realm's issuer to be `http://keycloak:8080/...`, and it
 * has to be: the issuer is one string that must resolve to the same Keycloak from the browser, the
 * Next server *and* Django, which rules out `localhost` — inside a container that is the container.
 * The https guard in `lib/oidc.ts` allows http only for loopback, so the documented setup could not
 * work at all until this existed.
 *
 * Server-only, and deliberately **not** `NEXT_PUBLIC_`: nothing about it belongs in the browser
 * bundle. It only ever *widens* which http issuers are acceptable while the app itself is on plain
 * http; `assertUsableEndpoint` keeps "the app is not on TLS" as an outer condition this cannot
 * reach, so an https deployment refuses an http issuer no matter how this is set.
 */
export const allowInsecureIssuer = memoize(() => process.env.OIDC_ALLOW_INSECURE_ISSUER === "1");

/**
 * Cookie names, `__Host-` prefixed when the deployment is https.
 *
 * The prefix is what forbids a `Domain` attribute, and a `Domain` attribute is the only way a
 * sibling subdomain can shadow a cookie this server minted. It also *mandates* `Secure`, which is
 * why it cannot be used on plain `http://localhost` (Safari rejects `Secure` there outright).
 *
 * The consequence for local development is worth stating plainly, because it is invisible: on bare
 * `localhost` this protection is absent, and cookies are scoped by host **ignoring the port**, so
 * every other loopback service shares them. That is a large part of why the session cookie carries
 * an opaque id rather than a token — see `lib/session.ts`.
 */
export const cookieNames = memoize(() => {
  const prefix = cookieSecure() ? "__Host-" : "";
  return {
    session: `${prefix}tiq_session`,
    csrf: `${prefix}tiq_csrf`,
    tx: `${prefix}tiq_tx`,
  } as const;
});
