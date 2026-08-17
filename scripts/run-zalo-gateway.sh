#!/bin/sh
set -eu

case "${ZALO_ENABLED:-false}" in
  1|true|TRUE|yes|YES|on|ON)
    exec node /app/zalo-gateway/dist/index.js
    ;;
  *)
    echo "[zalo] gateway disabled; set ZALO_ENABLED=true after configuring the session"
    exec tail -f /dev/null
    ;;
esac
