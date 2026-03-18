#!/bin/sh
set -e

# Generate runtime config for frontend
cat > /usr/share/nginx/html/sep-config.js <<'CONFIGEOF'
(function(){
  window.__SEP_CONFIG__ = Object.assign({}, window.__SEP_CONFIG__ || {}, {
    API_URL: "__SEP_API_URL__",
    API_BEARER_TOKEN: "__SEP_API_BEARER_TOKEN__",
    WS_URL: "__SEP_WS_URL__",
    WS_TOKEN: "__SEP_WS_TOKEN__",
    READ_ONLY: "__READ_ONLY__",
    ALLOW_KILL_TOGGLE: "__ALLOW_KILL_TOGGLE__",
  });
})();
CONFIGEOF

# Replace placeholders with env; leave blank to use same-origin /api and /ws
sed -i \
  -e "s#__SEP_API_URL__#${API_URL:-}#g" \
  -e "s#__SEP_API_BEARER_TOKEN__#${API_BEARER_TOKEN:-}#g" \
  -e "s#__SEP_WS_URL__#${WS_URL:-}#g" \
  -e "s#__SEP_WS_TOKEN__#${WS_TOKEN:-}#g" \
  -e "s#__READ_ONLY__#${READ_ONLY:-}#g" \
  -e "s#__ALLOW_KILL_TOGGLE__#${ALLOW_KILL_TOGGLE:-}#g" \
  /usr/share/nginx/html/sep-config.js

# Render nginx config from template
TEMPLATES_DIR="/opt/nginx/templates"
SERVER_NAME_DEFAULT="${SERVER_NAME:-mxbikes.xyz}"
BACKEND_UPSTREAM_DEFAULT="${BACKEND_UPSTREAM:-http://backend:8000}"
WS_UPSTREAM_DEFAULT="${WS_UPSTREAM:-http://websocket:8001}"
CERT_BASE_DEFAULT="${CERT_BASE:-/etc/letsencrypt/live/${SERVER_NAME_DEFAULT}}"

USE_LOCAL=0
if [ "${USE_LOCAL_NGINX:-}" = "1" ]; then
  USE_LOCAL=1
fi
# If TLS cert missing, prefer local HTTP config
if [ ! -f "${CERT_BASE_DEFAULT}/fullchain.pem" ]; then
  USE_LOCAL=1
fi

if [ "${USE_LOCAL}" = "1" ]; then
  SRC_TMPL="${TEMPLATES_DIR}/nginx.local.tmpl.conf"
  echo "Rendering local nginx config (HTTP only)"
else
  SRC_TMPL="${TEMPLATES_DIR}/nginx.tmpl.conf"
  echo "Rendering TLS nginx config for ${SERVER_NAME_DEFAULT}"
fi

if [ -f "${SRC_TMPL}" ]; then
  sed \
    -e "s#__SERVER_NAME__#${SERVER_NAME_DEFAULT}#g" \
    -e "s#__BACKEND_UPSTREAM__#${BACKEND_UPSTREAM_DEFAULT}#g" \
    -e "s#__WS_UPSTREAM__#${WS_UPSTREAM_DEFAULT}#g" \
    -e "s#__CERT_BASE__#${CERT_BASE_DEFAULT}#g" \
    "${SRC_TMPL}" > /etc/nginx/conf.d/default.conf
else
  echo "Template not found at ${SRC_TMPL}; using baked default.conf"
fi

echo "Starting nginx..."
exec nginx -g "daemon off;"
