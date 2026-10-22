/** @type {import('next').NextConfig} */
const nextConfig = {
  // The production image runs `.next/standalone/server.js` (#25, ADR-0022): `next build` traces the
  // files the server imports and copies only those, so the image carries no devDependencies and no
  // `next` CLI. `next dev` ignores this, so compose's dev server is unaffected.
  output: "standalone",
};

export default nextConfig;
