#!/bin/sh
set -eu

case "${ZALO_ENABLED:-false}" in
  1|true|TRUE|yes|YES|on|ON)
    heap_mb="${ZALO_NODE_HEAP_MB:-96}"
    case "$heap_mb" in ''|*[!0-9]*) heap_mb=96;; esac
    if [ "$heap_mb" -lt 64 ] || [ "$heap_mb" -gt 256 ]; then heap_mb=96; fi
    exec node --max-old-space-size="$heap_mb" --max-semi-space-size=8 /app/zalo-gateway/dist/index.js
    ;;
  *)
    echo "[zalo] gateway disabled; set ZALO_ENABLED=true after configuring the session"
    exec tail -f /dev/null
    ;;
esac
