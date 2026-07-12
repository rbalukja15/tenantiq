# Frontend image — the Next.js dev server for the local stack (#23).
FROM node:20-alpine

WORKDIR /app

# Install deps first for layer caching; the dev stack mounts ./frontend over /app for hot reload,
# with node_modules kept in an anonymous volume so the image's install isn't shadowed.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 3000

# Bind to 0.0.0.0 so the port is reachable from the host through the compose port mapping.
CMD ["npm", "run", "dev", "--", "-H", "0.0.0.0", "-p", "3000"]
