import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Component-test harness (#52).
 *
 * Every #18–#20 acceptance criterion is UI behaviour, so the suite has to be able to *render*
 * components and assert on what a user sees: hence jsdom + Testing Library, with the React plugin
 * so JSX transforms work in tests. `setupFiles` installs jest-dom matchers and the MSW server that
 * intercepts `fetch`, so tests exercise real data-fetching code paths instead of hand-rolled stubs.
 *
 * `include` deliberately covers **both** `tests/` and tests colocated next to components, so a
 * `Foo.test.tsx` written beside `Foo.tsx` in #19/#20 runs instead of being silently ignored.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    include: ["{app,tests,lib,components}/**/*.test.{ts,tsx}"],
    css: false,
    // Set here rather than in a setup file because `lib/config.ts` memoizes on first call: these
    // must be in place before any module is imported, or the first suite to touch config would fix
    // the values for every later one. The origins are deliberately *different* from each other and
    // from jsdom's own `localhost:3000`, so a request that should go to the API can never
    // accidentally match a handler registered for the app (#18).
    env: {
      API_BASE_URL: "http://backend:8000",
      APP_BASE_URL: "http://localhost:3000",
    },
  },
  resolve: {
    // fileURLToPath, not URL.pathname: the latter is percent-encoded, so a checkout path containing
    // a space resolves to a directory that does not exist and every "@/..." import fails.
    alias: { "@": fileURLToPath(new URL(".", import.meta.url)) },
  },
});
