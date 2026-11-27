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
# Ghim distro (trixie = đích hiện tại của tag 3.12-slim) để base không tự nhảy
# Debian khi Docker Hub đổi mặc định; Dependabot (ecosystem docker) theo dõi.
FROM python:3.12-slim-trixie

# PYTHONDONTWRITEBYTECODE chỉ cấm GHI .pyc lúc chạy; .pyc của code app được biên
# dịch sẵn lúc build (compileall bên dưới) và vẫn được ĐỌC.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MALLOC_ARENA_MAX=2 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

# ffmpeg: đổi giọng TTS sang OGG/Opus (tin thoại Telegram) và M4A/AAC (Zalo) cho
# /noi - services/voice_reply.py; chỉ chạy 1 tiến trình ngắn mỗi tin thoại.
RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=gateway /usr/local/bin/node /usr/local/bin/node

WORKDIR /app

# Chỉ PyPI. pip check bắt dependency thiếu/lệch ngay lúc build thay vì lúc import
# trên Render.
COPY requirements.txt ./
RUN python -m pip install -r requirements.txt \
    && python -m pip check

COPY . .
COPY --from=gateway /gw/dist ./zalo-gateway/dist
COPY --from=gateway /gw/node_modules ./zalo-gateway/node_modules
# Biên dịch sẵn ~260 module app (.dockerignore bỏ __pycache__/test/docs): mỗi lần
# supervisord chạy lại uvicorn không phải biên dịch lại từ đầu (import web nhanh gần
# gấp đôi). checked-hash: .pyc khớp theo hash nội dung, không theo mtime (build ghi
# lại mtime cũng không làm .pyc bị coi là cũ). Bỏ node_modules của gateway (không
# phải code Python). File lỗi cú pháp làm build hỏng ngay tại đây.
RUN chmod +x /app/scripts/run-zalo-gateway.sh \
    && mkdir -p /app/media \
    && python -m compileall -q -j 0 --invalidation-mode checked-hash -x '/zalo-gateway/' /app

# Chạy root: xem ghi chú đầu supervisord.conf (Render không hỗ trợ đổi user trong
# supervisord - HOME không đổi, root không được kill tiến trình khác uid).
EXPOSE 10000
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf"]
