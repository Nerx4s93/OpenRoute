FROM nginx:1.27-alpine

# Конфиг «стены»: единая точка входа, маршрутизация на gateway/web-admin/web-admin-ui.
COPY nginx.conf /etc/nginx/nginx.conf
COPY proxy_params_edge.conf /etc/nginx/proxy_params_edge.conf

# Базовый образ — root-вариант nginx:1.27-alpine (unprivileged не используется).
# Healthcheck задан в docker-compose.yml и вызывает wget, который есть в alpine.
EXPOSE 8000
