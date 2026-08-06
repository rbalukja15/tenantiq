import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescript from "eslint-config-next/typescript";

/**
 * ESLint 9 flat config (#52), replacing the legacy `.eslintrc.json` + `.eslintignore`.
 *
 * `eslint-config-next@16` ships native flat-config arrays, so they are spread in directly — no
 * `FlatCompat` bridge (which in fact crashes on this package). Two things the Next presets do *not*
 * give us, which the project's conventions require, are added explicitly below:
 *
 * - **`no-explicit-any` / unused vars.** `next/typescript` registers the TypeScript parser and plugin
 *   but ships an empty rule set, so "TypeScript strict, no `any`" (CLAUDE.md) was enforced by nothing.
 * - **Severity that actually gates.** Next's accessibility rules are *warnings*, and `eslint .` exits
 *   0 on warnings — so they could never fail CI. The `lint` script runs with `--max-warnings 0`, which
 *   makes every warning a build failure, and these rules are set to `error` outright.
 */
const config = [
  {
    // Flat config has no `.eslintignore`; ignore patterns live here.
    ignores: [".next/**", "node_modules/**", "next-env.d.ts", "coverage/**"],
  },
  ...coreWebVitals,
  ...typescript,
  {
    files: ["**/*.{ts,tsx,mts}"],
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
];

export default config;
