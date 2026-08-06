import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, expect } from "vitest";

import { server } from "./msw";

/**
 * Requests no handler covered. MSW's `onUnhandledRequest: "error"` is **not** enough on its own: it
 * rejects the request, which application code then catches and turns into its own error state — so a
 * test asserting "shows an error" would pass whether the API returned 500 or nobody mocked anything
 * at all. Recording them here and failing in `afterEach` makes an unmocked request a test failure,
 * which is what stops a test from lying about what it exercised.
 */
const unhandled: string[] = [];

beforeAll(() => {
  server.events.on("request:unhandled", ({ request }) => {
    unhandled.push(`${request.method} ${request.url}`);
  });
  server.listen({ onUnhandledRequest: "error" });
});

afterEach(() => {
  cleanup(); // unmount React trees so one test's DOM can't leak into the next
  server.resetHandlers(); // per-test handlers only, so tests stay order-independent

  const seen = unhandled.splice(0, unhandled.length);
  expect(seen, `unmocked request(s) — add an MSW handler:\n  ${seen.join("\n  ")}`).toEqual([]);
});

afterAll(() => {
  server.events.removeAllListeners();
  server.close();
});
