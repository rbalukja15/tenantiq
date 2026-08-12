/**
 * Shared constants and helpers for the BFF suites (#18).
 *
 * The base values live in `vitest.config.mts` under `test.env`, so they are set before any module is
 * imported. That matters because `lib/config.ts` memoizes on first call: a test that set them at
 * runtime would race whichever suite happened to call `apiBaseUrl()` first.
 */

export const API_ORIGIN = "http://backend:8000";
export const APP_ORIGIN = "http://localhost:3000";

/**
 * Find the literal `Set-Cookie` line for `name`, or `undefined`.
 *
 * Exists because the obvious inline check is a trap: `/tiq_session=.*Max-Age=0|1970/` parses as
 * `(tiq_session=.*Max-Age=0)|(1970)`, so the second branch matches the `1970` in *any* cookie's
 * expiry — including the CSRF cookie's. Two "the session cookie was cleared" assertions passed that
 * way while the session cookie was never cleared at all.
 */
export function setCookie(response: Response, name: string): string | undefined {
  return response.headers.getSetCookie().find((line) => line.startsWith(`${name}=`));
}

/** True when this exact cookie is being expired (empty value plus a past expiry / zero age). */
export function isCleared(response: Response, name: string): boolean {
  const line = setCookie(response, name);
  if (line === undefined) return false;
  const value = line.slice(name.length + 1).split(";")[0];
  return value === "" && /Max-Age=0|Expires=Thu, 01 Jan 1970/.test(line);
}

/**
 * Load a module with a *different* environment — for the handful of assertions that depend on the
 * deployment being https (the `__Host-` cookie prefix, the `Secure` flag, endpoint scheme rules).
 *
 * Uses `vi.resetModules()` plus a dynamic import so the memoized config is genuinely rebuilt; the
 * previous environment is restored afterwards so suite order stays irrelevant.
 */
export async function withEnv<T>(
  overrides: Record<string, string>,
  load: () => Promise<T>,
): Promise<T> {
  const { vi } = await import("vitest");
  const previous = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(overrides)) {
    previous.set(key, process.env[key]);
    process.env[key] = value;
  }
  vi.resetModules();
  try {
    return await load();
  } finally {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    vi.resetModules();
  }
}
