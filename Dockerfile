FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    MALLOC_ARENA_MAX=2 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm supervisor ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --extra-index-url https://vnstocks.com/api/simple -r requirements.txt
# Render 512 MB: use the remote CDP worker. Local engines are opt-in at build time.
ARG INSTALL_LOCAL_BROWSERS=false
ENV SHOPEE_LOCAL_BROWSERS_INSTALLED=$INSTALL_LOCAL_BROWSERS
RUN if [ "$INSTALL_LOCAL_BROWSERS" = "true" ]; then \
      playwright install --with-deps --only-shell chromium \
      && playwright install --with-deps webkit; \
    fi \
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
