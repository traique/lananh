#!/bin/sh
set -eu

# Trần heap V8 cho gateway Zalo (Render free 512 MB dùng chung với Python):
# - ZALO_NODE_HEAP_MB (64-256, mặc định 96): --max-old-space-size.
# - ZALO_NODE_OPTIMIZE_SIZE (mặc định 1): --optimize-for-size - V8 ưu tiên bộ nhớ
#   hơn tốc độ (semi-space nhỏ hơn, GC sớm hơn); đo zca-js 2.1.2 trên Node 22:
#   RSS sau khi nạp 86 -> 75 MB. 0 = tắt nếu thấy gateway chậm.
# - ZALO_NODE_EXTRA_FLAGS: cờ node thêm (vd "--max-semi-space-size=4"), để trống.
case "${ZALO_ENABLED:-false}" in
  1|true|TRUE|yes|YES|on|ON)
    heap_mb="${ZALO_NODE_HEAP_MB:-96}"
    case "$heap_mb" in ''|*[!0-9]*) heap_mb=96;; esac
    if [ "$heap_mb" -lt 64 ] || [ "$heap_mb" -gt 256 ]; then heap_mb=96; fi
    size_flag=""
    case "${ZALO_NODE_OPTIMIZE_SIZE:-1}" in
      0|false|FALSE|no|NO|off|OFF) ;;
      *) size_flag="--optimize-for-size" ;;
    esac
    # shellcheck disable=SC2086 # tách cờ thêm theo khoảng trắng là có chủ ý
    exec node --max-old-space-size="$heap_mb" --max-semi-space-size=8 $size_flag \
      ${ZALO_NODE_EXTRA_FLAGS:-} /app/zalo-gateway/dist/index.js
    ;;
  *)
    echo "[zalo] gateway disabled; set ZALO_ENABLED=true after configuring the session"
    exec tail -f /dev/null
    ;;
esac
