FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm supervisor ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt
# Render Free: install only Chromium headless shell, not the full headed browser.
# This keeps the image/runtime footprint lower while matching Playwright exactly.
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY zalo-gateway/package.json zalo-gateway/package-lock.json zalo-gateway/tsconfig.json ./zalo-gateway/
RUN cd zalo-gateway && npm ci

COPY zalo-gateway/src ./zalo-gateway/src
RUN cd zalo-gateway \
    && npm run build \
    && npm prune --omit=dev \
    && npm cache clean --force

COPY . .
RUN chmod +x /app/scripts/run-zalo-gateway.sh

EXPOSE 10000
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf"]
