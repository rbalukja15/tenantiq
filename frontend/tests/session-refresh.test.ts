import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { ensureFreshSession } from "@/lib/session-refresh";
import { createSession, getSession, resetSessionsForTest, type SessionRecord } from "@/lib/session";

import { server } from "./msw";

const ISSUER = "https://idp.test/realms/acme";
const TOKEN_ENDPOINT = `${ISSUER}/protocol/openid-connect/token`;

afterEach(() => resetSessionsForTest());

function seed(overrides: Partial<SessionRecord> = {}): { id: string; record: SessionRecord } {
  const record: SessionRecord = {
    accessToken: "old-token",
    refreshToken: "refresh-token",
    expiresAt: Math.floor(Date.now() / 1000) - 1, // expired, so a refresh is due
    tokenEndpoint: TOKEN_ENDPOINT,
    issuer: ISSUER,
    clientId: "tenantiq-acme",
    tenantSlug: "acme",
    createdAt: Date.now(),
    ...overrides,
  };
  return { id: createSession(record), record };
}

describe("ensureFreshSession", () => {
  it("leaves a session that is still fresh completely alone", async () => {
    // No MSW handler registered: a token still within its lifetime must cause no network call.
    const { id, record } = seed({ expiresAt: Math.floor(Date.now() / 1000) + 3600 });

    const outcome = await ensureFreshSession(id, record);

    expect(outcome).toMatchObject({ status: "ok", refreshed: false });
  });

  it("renews an expiring token and stores the new one", async () => {
    server.use(
      http.post(TOKEN_ENDPOINT, () =>
        HttpResponse.json({ access_token: "new-token", expires_in: 300 }),
      ),
    );
    const { id, record } = seed();

    const outcome = await ensureFreshSession(id, record);

    expect(outcome.status).toBe("ok");
    expect(outcome.status === "ok" && outcome.record.accessToken).toBe("new-token");
    expect(getSession(id)?.accessToken).toBe("new-token");
  });

  it("keeps the existing refresh token when the provider does not rotate it", async () => {
    // Overwriting with `undefined` would silently end the session at the next refresh.
    server.use(
      http.post(TOKEN_ENDPOINT, () =>
        HttpResponse.json({ access_token: "new-token", expires_in: 300 }),
      ),
    );
    const { id, record } = seed();

    await ensureFreshSession(id, record);

    expect(getSession(id)?.refreshToken).toBe("refresh-token");
  });

  it("ends the session when the provider rejects the refresh token", async () => {
    server.use(
      http.post(TOKEN_ENDPOINT, () =>
        HttpResponse.json({ error: "invalid_grant" }, { status: 400 }),
      ),
    );
    const { id, record } = seed();

    expect(await ensureFreshSession(id, record)).toEqual({ status: "expired" });
    expect(getSession(id)).toBeUndefined();
  });

  it("ends the session when there is no refresh token to use", async () => {
    const { id, record } = seed({ refreshToken: undefined });

    expect(await ensureFreshSession(id, record)).toEqual({ status: "expired" });
    expect(getSession(id)).toBeUndefined();
  });

  it("keeps the session alive when the provider is merely unreachable", async () => {
    // The distinction that matters: `invalid_grant` means the credential is dead, but a network
    // failure or a 502 says nothing about it. Treating them alike would log every user out during a
    // brief IdP blip, with no way back except signing in again.
    server.use(http.post(TOKEN_ENDPOINT, () => HttpResponse.error()));
    const { id, record } = seed();

    const outcome = await ensureFreshSession(id, record);

    expect(outcome.status).toBe("unavailable");
    expect(getSession(id)).toBeDefined();
  });

  it("treats a provider 5xx as retryable, not as a rejected credential", async () => {
    server.use(http.post(TOKEN_ENDPOINT, () => new HttpResponse(null, { status: 503 })));
    const { id, record } = seed();

    expect((await ensureFreshSession(id, record)).status).toBe("unavailable");
    expect(getSession(id)).toBeDefined();
  });
});
