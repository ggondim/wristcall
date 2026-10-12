# Build on the runner's own platform (the output is plain files); only the nginx stage is per architecture.
FROM --platform=$BUILDPLATFORM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# The service worker build reads dist/.vite/manifest.json; the site must not publish it.
RUN npm run build && npm run check:dist && rm -rf dist/.vite

FROM nginxinc/nginx-unprivileged:1.30-alpine
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY nginx/security-headers.conf /etc/nginx/security-headers.conf
COPY --chmod=755 docker/40-wristcall-config.sh /docker-entrypoint.d/40-wristcall-config.sh
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
