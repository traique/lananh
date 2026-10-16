# Stage 1: build gateway Zalo bằng Node 22 (khớp CI và @types/node ^22; Debian
# bookworm chỉ có Node 18 qua apt).
FROM node:22-bookworm-slim AS gateway
WORKDIR /gw
COPY zalo-gateway/package.json zalo-gateway/package-lock.json zalo-gateway/tsconfig.json ./
RUN npm ci --no-audit --no-fund
COPY zalo-gateway/src ./src
RUN npm run build \
    && npm prune --omit=dev \
    && npm cache clean --force

# Stage 2: runtime Python + binary node copy từ stage trên (không cài npm).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MALLOC_ARENA_MAX=2 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app

COPY --from=gateway /usr/local/bin/node /usr/local/bin/node

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --extra-index-url https://vnstocks.com/api/simple -r requirements.txt

COPY . .
COPY --from=gateway /gw/dist ./zalo-gateway/dist
COPY --from=gateway /gw/node_modules ./zalo-gateway/node_modules
RUN chmod +x /app/scripts/run-zalo-gateway.sh \
    && mkdir -p /app/media \
    && chown -R app:app /app

# supervisord chạy root để đổi user cho từng program (user=app trong supervisord.conf).
EXPOSE 10000
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf"]
