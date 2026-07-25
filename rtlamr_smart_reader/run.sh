#!/usr/bin/with-contenv bashio
set -euo pipefail

export MQTT_SERVICE_HOST=""
export MQTT_SERVICE_PORT=""
export MQTT_SERVICE_USERNAME=""
export MQTT_SERVICE_PASSWORD=""

if MQTT_SERVICE_HOST="$(bashio::services mqtt 'host' 2>/dev/null)"; then
  export MQTT_SERVICE_HOST
  MQTT_SERVICE_PORT="$(bashio::services mqtt 'port' 2>/dev/null || true)"
  MQTT_SERVICE_USERNAME="$(bashio::services mqtt 'username' 2>/dev/null || true)"
  MQTT_SERVICE_PASSWORD="$(bashio::services mqtt 'password' 2>/dev/null || true)"
  export MQTT_SERVICE_PORT MQTT_SERVICE_USERNAME MQTT_SERVICE_PASSWORD
  bashio::log.info "Using MQTT service from Supervisor at ${MQTT_SERVICE_HOST}:${MQTT_SERVICE_PORT:-1883}"
else
  bashio::log.warning "MQTT service details were not available from Supervisor; falling back to add-on options or core-mosquitto."
fi

exec python3 /app/smart_rtlamr.py
