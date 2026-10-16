#!/bin/bash
# SessionStart hook cho phiên Claude Code cloud: cài dependency Python + Node
# để ruff/pytest và check/test gateway chạy được ngay. Idempotent; chỉ chạy
# trong môi trường remote (máy local của chủ repo tự quản lý venv).
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

PY=python3.12
command -v "$PY" >/dev/null 2>&1 || PY=python3

if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
# vnstock/vnai nằm ở kho riêng; nếu kho đó không với tới được thì vẫn cài phần
# còn lại (test liên quan vnstock tự skip).
if ! .venv/bin/pip install -q --extra-index-url https://vnstocks.com/api/simple -r requirements-dev.txt; then
  echo "[hook] cài vnstock từ kho riêng thất bại - cài phần còn lại từ PyPI"
  grep -v '^vnstock\|^vnai\|^-r \|^#' requirements.txt > /tmp/req-base.txt
  grep -v '^-r \|^#' requirements-dev.txt >> /tmp/req-base.txt
  .venv/bin/pip install -q -r /tmp/req-base.txt
fi

if [ -f zalo-gateway/package.json ]; then
  (cd zalo-gateway && npm install --no-audit --no-fund --silent)
fi
if [ -f test/sql_support/package.json ]; then
  (cd test/sql_support && npm install --no-audit --no-fund --silent)
fi

echo 'export PATH="$CLAUDE_PROJECT_DIR/.venv/bin:$PATH"' >> "$CLAUDE_ENV_FILE"
echo "[hook] sẵn sàng: ruff check . && pytest -q; cd zalo-gateway && npm run check && npm test"
