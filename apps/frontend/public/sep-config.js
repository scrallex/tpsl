// Default runtime config placeholder. In production, the Docker entrypoint
// overwrites this file with actual values from environment variables.
window.__SEP_CONFIG__ = Object.assign({
  API_URL: undefined,
  API_BEARER_TOKEN: undefined,
  API_TOKEN: undefined,
  WS_URL: undefined,
  WS_TOKEN: undefined,
  READ_ONLY: undefined,
  ALLOW_KILL_TOGGLE: undefined,
}, window.__SEP_CONFIG__ || {});
