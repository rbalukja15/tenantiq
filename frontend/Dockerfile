# Frontend images, two targets from one file.
#
#   dev         the Next.js dev server for the local stack (#23). Compose builds this target.
#   production  `next build` with standalone output, run as uid 1000 (#25, ADR-0022). The default
#               target, so a plain `docker build` produces what the cluster runs.
#
# Node 22 LTS: Node 20 is end-of-life, and the #52 toolchain (vitest 4, jsdom 30) requires >= 22.
FROM node:22-alpine AS deps

WORKDIR /app

# Install deps first for layer caching.
COPY package.json package-lock.json ./
RUN npm ci


FROM deps AS dev

# The dev stack mounts ./frontend over /app for hot reload, with node_modules kept in an anonymous
# volume so the install above isn't shadowed.
COPY . .

EXPOSE 3000

# Bind to 0.0.0.0 so the port is reachable from the host through the compose port mapping.
CMD ["npm", "run", "dev", "--", "-H", "0.0.0.0", "-p", "3000"]


FROM deps AS build

COPY . .

# No environment is needed or wanted here. lib/config.ts reads API_BASE_URL and APP_BASE_URL when a
# request arrives, not at build time, so one image serves every environment; nothing is NEXT_PUBLIC_.
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build


FROM node:22-alpine AS production

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

# The standalone server is the traced subset of node_modules that the app imports, about a tenth of
# a full install. It does not include the static assets, which are copied next to it so the server
# can serve them; a `public/` directory, if one is ever added, needs the same copy. Both stay owned by
# root, so the user that runs the server can read its code but not rewrite it.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static

# uid 1000 is the image's `node` user and the uid frontend.yaml runs it as.
USER node

EXPOSE 3000

# server.js listens on $HOSTNAME, and that variable belongs to the platform: Docker, Kubernetes and
# ECS set it to the container's own name when it starts, and some of them override an image's ENV
# to do it (vercel/next.js#58657). Bound to that name, the server refuses connections on localhost,
# which is where `kubectl port-forward` connects. The command is the one place none of them touch.
# `exec` keeps node as PID 1, so the SIGTERM a rollout sends reaches it.
CMD ["sh", "-c", "HOSTNAME=0.0.0.0 exec node server.js"]
