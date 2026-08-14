import { redirect } from "next/navigation";

import { Callout } from "@/app/components/ui/Callout";
import { apiBaseUrl } from "@/lib/config";

import styles from "./TenantHome.module.css";

/**
 * The subset of `GET /api/me` this view needs (see the backend's `MeView`).
 *
 * `display_name` is the only field here a person is ever shown. `username` is deliberately **not**
 * rendered: it is the synthesized `<sub>.<issuer-hash>` identity key, and printing it is how the
 * shell came to greet Alice as `c76c642e-…-8e0ad55a57f4.6fa97fcf2c06` (#84). It stays in the type
 * because the endpoint returns it and mislabelling the payload would just move the confusion.
 */
type Me = {
  username: string;
  display_name: string;
  email: string;
  tenant: { id: string; slug: string; name: string };
};

/**
 * The signed-in landing view: who you are and which tenant you are in (#18).
 *
 * **Calls Django directly, with the token passed in as an argument.** A Server Component must never
 * fetch its own app's `/api/*` proxy: a server-side `fetch` is a fresh outbound request that does
 * not inherit the incoming `Cookie` header, so the proxy would answer 401 on every render and a
 * perfectly logged-in user would see the logged-out branch forever. Taking `accessToken` as a prop
 * also keeps this component a plain function of its arguments, so a test can render it twice with
 * two different tenants' tokens and prove no state is shared between them.
 *
 * `cache: "no-store"` is load-bearing, not decoration: an `Authorization`-bearing GET is not
 * unconditionally excluded from Next's fetch cache, and one tenant's identity landing in a shared
 * cache entry is precisely the cross-tenant leak this project exists to make impossible.
 */
export async function TenantHome({ accessToken }: { accessToken: string }) {
  const response = await fetch(new URL("/api/me", apiBaseUrl()), {
    headers: { authorization: `Bearer ${accessToken}` },
    cache: "no-store",
  });

  if (response.status === 401) redirect("/login?error=session_expired");
  if (!response.ok) {
    // Generic on purpose: an API failure reason is not something to render back to the browser.
    return <Callout tone="error">Could not load your session.</Callout>;
  }

  const me = (await response.json()) as Me;

  return (
    <section className={styles.wrap}>
      <h1 className={styles.tenant}>{me.tenant.name}</h1>
      {/* No name claim in the token is a real case — a minimal client scope sends only `sub` — and
          "Signed in" without one is the honest rendering. Falling back to `username` here is
          precisely the bug (#84); falling back to the email would put an address on screen that the
          IdP never offered as a name. */}
      <p className={styles.identity}>
        {me.display_name ? (
          <>
            Signed in as <strong>{me.display_name}</strong>
          </>
        ) : (
          "Signed in"
        )}
      </p>
      <p className={styles.scope}>
        <span className={styles.slug}>{me.tenant.slug}</span>
        Every document and answer you see is scoped to this workspace.
      </p>
    </section>
  );
}
