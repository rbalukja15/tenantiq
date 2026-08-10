const MESSAGES: Record<string, string> = {
  retry: "That sign-in attempt could not be completed. Please try again.",
  unknown_tenant: "We could not find that workspace. Check the name and try again.",
  session_expired: "Your session has ended. Please sign in again.",
  unavailable: "Sign-in is temporarily unavailable. Please try again in a moment.",
};

/**
 * The login form (#18).
 *
 * A plain `<form method="post">` rather than a link, because starting a login is a state-changing
 * action: `SameSite=Lax` permits top-level GET navigations, so a GET login endpoint would let any
 * site push a visitor into a tenant of the attacker's choosing. The handler additionally requires a
 * matching `Origin`, which a cross-site POST cannot produce.
 *
 * The tenant *slug* is asked for because per-tenant OIDC configuration lives in the database and the
 * browser has no token yet — the slug is what the public discovery endpoint resolves into a realm
 * (ADR-0013 §2).
 */
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;
  const message = error ? MESSAGES[error] : undefined;

  return (
    <main>
      <h1>Sign in to TenantIQ</h1>
      {message ? <p role="alert">{message}</p> : null}
      <form method="post" action="/api/auth/login">
        <label htmlFor="tenant">Workspace</label>
        <input
          id="tenant"
          name="tenant"
          type="text"
          required
          autoComplete="organization"
          placeholder="acme"
        />
        <button type="submit">Continue</button>
      </form>
    </main>
  );
}
