# build stage: node toolchain compiles the SPA
FROM node:20-alpine AS build
WORKDIR /app
COPY web/package.json web/package-lock.json ./
# npm ci installs exactly what package-lock.json pins — reproducible builds
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# serve stage: static assets + the /v1 reverse proxy
FROM nginx:alpine
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
