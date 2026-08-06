import { setupServer } from "msw/node";

/**
 * The shared MSW server (#52).
 *
 * Tests mock at the **network** boundary rather than by monkey-patching modules, so the component
 * under test runs its real fetch/parsing code — the part most likely to break — and a test can only
 * pass by actually talking to the (fake) API. Handlers are registered per test with
 * `server.use(...)`; there are deliberately no default handlers, so an unmocked request is a loud
 * failure (see `onUnhandledRequest` in `setup.ts`) rather than a silent hang.
 */
export const server = setupServer();
