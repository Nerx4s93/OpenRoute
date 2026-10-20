FROM nginx:1.27-alpine

# Конфиг «стены»: единая точка входа, маршрутизация на gateway/web-admin/web-admin-ui.
COPY nginx.conf /etc/nginx/nginx.conf
COPY proxy_params_edge.conf /etc/nginx/proxy_params_edge.conf

# nginx пишет PID-файл в /var/cache; unprivileged-образ заботится сам,
# но healthcheck ниже требует wget из образа — он есть в alpine.
EXPOSE 8000
