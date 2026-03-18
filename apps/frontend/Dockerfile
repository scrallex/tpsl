FROM node:20 AS build
WORKDIR /app
COPY package*.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.29.1-alpine
# Install curl for healthchecks
RUN apk add --no-cache curl
COPY --from=build /app/dist /usr/share/nginx/html
# Use consolidated nginx.conf (was nginx.hotband.conf)
COPY nginx.conf /etc/nginx/conf.d/default.conf
# Shared include fragments
COPY nginx/includes /etc/nginx/includes
# Override global nginx.conf to raise worker_connections and tune proxy
COPY nginx.global.conf /etc/nginx/nginx.conf
# Provide templated configs for runtime rendering
COPY nginx.tmpl.conf /opt/nginx/templates/nginx.tmpl.conf
COPY nginx.local.tmpl.conf /opt/nginx/templates/nginx.local.tmpl.conf

# Entrypoint writes runtime config from env to /usr/share/nginx/html/sep-config.js
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENV API_URL="" \
    API_BEARER_TOKEN="" \
    WS_URL="" \
    WS_TOKEN="" \
    READ_ONLY="" \
    ALLOW_KILL_TOGGLE=""

CMD ["/bin/sh", "/docker-entrypoint.sh"]
