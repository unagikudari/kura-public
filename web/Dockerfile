# kura web. Build context: repository root. The origin is fixed at build time (vite.config.ts), so
# compose.yaml passes ORIGIN as a build argument; nothing secret is in the image.
FROM node:24-slim AS build
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY VERSION /app/
COPY web/ .
ARG ORIGIN
RUN test -n "$ORIGIN" && ORIGIN="$ORIGIN" npm run build

FROM node:24-slim
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci --omit=dev
COPY --from=build /app/web/build build
USER node
ENV NODE_ENV=production PORT=3000 HOST=0.0.0.0 BODY_SIZE_LIMIT=2149580800
EXPOSE 3000
CMD ["node", "build/index.js"]
