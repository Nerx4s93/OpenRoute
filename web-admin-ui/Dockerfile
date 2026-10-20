# Static React SPA for the OpenRoute admin panel.
# Build stage uses Node; the runtime image contains only nginx + dist.
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine

# The unprivileged image listens on 8080 by default and runs as uid 101.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
