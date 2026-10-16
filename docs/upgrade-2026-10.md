# Đợt nâng cấp toàn diện 10/2026

Kết quả kiểm tra: `ruff check .` sạch (đã bật thêm bugbear `B` + `ASYNC`);
`pyright` 0 error; `pytest -q` 1062 passed, 25 skipped; `zalo-gateway`
check/format/test 45/45; `pip-audit` sạch sau khi nâng `fastapi`/`lxml`;
`npm audit --omit=dev` 0 lỗ hổng.

## Việc cần làm khi deploy

1. **Migration 015** tự chạy ở lần dùng DB đầu tiên: thêm cột `outcome`,
   `latency_ms`, `stage`, `prompt_chars` vào `provider_calls`; đổi `id` của
   `provider_calls`/`user_calls` sang BIGINT; xoá 3 bảng không còn dùng
   (`affiliate_short_links`, `telegram_processed_updates`, `zoom_processed_events`).
   Không cần thao tác tay.
2. **Dockerfile mới** (multi-stage): gateway build bằng Node 22 (khớp CI), runtime
   copy binary `node` + `dist/` + `node_modules`; hai tiến trình chạy dưới user
   `app` (không root). `supervisord` chờ 40s khi tắt (trước 10s, SIGKILL giữa lúc
   drain webhook). Render build lại tự động.
3. **Biến môi trường mới đều có mặc định** (xem bảng README "Cấu hình"):
   `MEMORY_UPDATE_TIMEOUT_SEC`, `RAG_CLEAN_TOTAL_TIMEOUT_SEC`,
   `PROMPT_BUDGET_CHARS` + 3 trần khối, `DATABASE_SSL_MODE`/`DATABASE_SSL_CA`,
   `SETTINGS_ENC_KEY_OLD`, `SETTINGS_REJECT_PLAINTEXT`, `ENABLE_PORTFOLIO_ALERTS`,
   `FACEBOOK_MAX_MEDIA_MB`, `STOCK_EXTRA_HOLIDAYS`, `STOCK_POLICY_PARAMS_PATH`.
4. `ZALO_BOT_ACCOUNT_ID` **không còn cần đặt** khi đã đăng nhập Zalo qua `/zalo`:
   job nền lấy `accountId` từ session. Nếu trước đây bạn đã đặt đúng id số thì giữ
   nguyên cũng được.
5. Nên đặt `DATABASE_SSL_MODE=verify-full` kèm `DATABASE_SSL_CA` trỏ tới file CA
   Supabase (tải ở Project Settings → Database) khi có thời gian kiểm thử; mặc
   định vẫn là `require` như cũ nên không bắt buộc.
6. Nâng dependency theo `pip-audit` (DB advisory mới nhất trên CI): `fastapi 0.110
   → 0.143.2` (starlette 1.8), `lxml 6.0.2 → 6.1.3`, `cryptography 42.0.0 →
   50.0.2`, `python-multipart 0.0.9 → 0.0.32`, `pillow 12.1.1 → 12.3.0`, `jinja2
   3.1.4 → 3.1.6`, `python-dotenv 1.0.1 → 1.2.4`. Toàn bộ test pass trên bộ mới.

## Lỗi đã sửa (đã xác nhận trong code trước khi sửa)

| # | Lỗi | Sửa |
|---|---|---|
| 1 | Tổng kết nhóm Zalo và bản tin sáng dùng `account_id` từ env (`zalo-bot`) trong khi gateway dùng id số thật → digest không thấy nhóm, tin nhóm không được dọn, bản tin sáng không ai gửi | `zalo_repository.resolve_active_account_id()`: session → env tường minh → tài khoản nhiều nhóm nhất |
| 2 | Điều chỉnh cổ tức chỉ chạy khi gap ≥ 12% → cổ tức tiền 3-6% không bao giờ được điều chỉnh | Fetch lịch quyền cho mọi chuỗi ≥ 60 bar (cache 24h/mã); ex-date chấp nhận rơi vào cuối tuần/lễ hoặc nguồn ghi ngày chốt quyền |
| 3 | DNSE trả 2 bar cùng ngày (27/12/2022) → `validate_ohlcv` từ chối cả chuỗi dài, backtest không có dữ liệu | Gộp bar cùng ngày giao dịch (high max, low min, close cuối, volume tổng) |
| 4 | Nghỉ Tết 9-10 ngày bị coi là "dữ liệu cũ" → chặn BUY toàn thị trường | `stock/calendar.py`: stale khi thiếu > 5 PHIÊN đã kết thúc |
| 5 | Gateway Zalo mất kết nối chỉ `api = null`, không kết nối lại, không báo ai; gửi trả lời lỗi thì mất hẳn | Reconnect backoff, retry gửi 3 lần, `POST /internal/zalo/gateway-state` → `alerts.send` cho chủ bot (báo khi ĐỔI trạng thái) |
| 6 | Deadline lồng: nhánh cổ phiếu + chat có thể tới 400s > 240s gateway chờ; cập nhật trí nhớ và Agnes/utility ngoài `bounded()`; `/ragxuly` khoá chat chủ bot hàng giờ | Một `turn_deadline()` cho cả lượt; `generate_utility_json`, Agnes qua `bounded()`; trí nhớ nền có trần riêng + semaphore 2; `/ragxuly` chạy nền ngoài khoá, trần tổng 15 phút |
| 7 | Cảnh báo Zoom chưa pair không giới hạn → ai nhắn được bot là spam được chủ bot | `zoom.notify_unpaired` dedup theo jid, qua `core.alerts` (có trần) |
| 8 | `compare_digest(str, str)` → header non-ASCII gây 500 thay vì 403 | So bytes ở webhook Telegram và bridge Zalo; guard payload Zoom không phải dict |
| 9 | Redeploy SIGKILL sau 10s; lease inbox giữ 5 phút sau shutdown | `stopwaitsecs=40`; trả lease về PENDING ngay khi task bị huỷ do shutdown |
| 10 | Prompt không có ngân sách (persona 11k + lịch sử không cắt + trí nhớ 17k + grounding) | `ai/prompt_budget.py`, lịch sử đọc DB 1 lần/lượt (trước 3-4 lần) |
| 11 | Provider đang cooldown 429 vẫn bị gọi lại cuối vòng | Chỉ 9Router (chết vì timeout) được thử lại làm cứu cánh |
| 12 | Gate tra web quá rộng ("tin anh", "giá như", "ok?") → 6 lượt AI vô ích | Marker theo từ + loại câu chào ngắn |
| 13 | `facebook_post_media` chỉ chốt theo SỐ bài, có thể vượt 500 MB Supabase | Trần byte `FACEBOOK_MAX_MEDIA_MB` (xoá bài chờ cũ nhất, rồi lưu bài không ảnh) |
| 14 | Daily digest không có guard "đã gửi hôm nay" lẫn chạy bù; bản tin sáng không chạy bù khi lỡ giờ | Guard lưu DB + khung gửi bù 3 giờ cho cả hai |
| 15 | `/help` thiếu danh mục (`/danhmuc`…), `/zalo`, `/fb_market`, `/fb_reconcile`, `/anh`, Zalo users… | Viết lại đầy đủ cho Telegram và Zalo/Zoom, nhóm theo chủ đề |
| 16 | `find_unknown_prices` không bắt "25,5"/"25.5k"; persona chủ bot không bị kiểm số lạ | Hiểu giá viết tắt nghìn đồng (trừ ngữ cảnh RSI/ADX/P/E…); persona chủ bot gắn cảnh báo mức giá không có trong dữ liệu |
| 17 | Khoá Fernet không xoay được; log chỉ che 6 secret; `ssl=require` không xác thực chứng chỉ | `MultiFernet` + `SETTINGS_ENC_KEY_OLD`; che mọi secret cấu hình; `DATABASE_SSL_MODE` |

## Thay đổi kiến trúc

- `services/channel_dispatch.py`: Zalo và Zoom dùng chung chuỗi cache →
  `/fb_*` ngoài khoá → khoá lượt → lệnh nhóm → chat (handler tiêm vào, test
  monkeypatch như cũ). `handlers/zalo_bridge_commands.py` sinh 20 handler từ bảng
  lệnh thay cho 20 wrapper.
- `stock/policy.py`: `PolicyParams` nạp từ `stock/policy_params.json`
  (`apply_params()` cho backtest grid); target measured-move cho breakout đỉnh mới.
- `services/portfolio_alerts.py`: job cảnh báo danh mục cuối phiên.
- `ai/orchestrator.utility_json()`: Groq `json_object` → Gemini utility; ghi
  `provider_calls` cho CẢ lượt lỗi kèm `latency_ms`, `outcome`, `stage`
  (`core.database.provider_outcomes()` cho trang admin).
- `services/memory_service.select_relevant_memory()`: retrieval giữa phiên bằng
  token trùng bỏ dấu (không cần pgvector).
- Dọn dead code: `_background_tasks` trong `web.py`, `claim_telegram_update`,
  `claim_zoom_event`, short-link `/r/{token}` + bảng.
- Hạ tầng dev: `test/conftest.py` (chặn mạng thật), `pyrightconfig.json`, ruff
  `B`/`ASYNC`, CI thêm pyright + pip-audit/npm audit + docker build + đủ 3 file
  PGlite, Dependabot, SessionStart hook cho phiên Claude Code cloud.
- Trang `/admin`: viết lại giao diện (xem README mục Trang quản trị).

## Backtest offline và `stock/data/backtest_stats.json`

File stats trước đây **không tồn tại** (dòng "thống kê setup" trong prompt luôn
rỗng). Lần này chạy offline bằng DNSE trực tiếp trên 183 mã trong
`stock/sector.py` (5 năm, concurrency 3, không cần vnstock):

| Lần chạy | Mẫu | Lệnh | Breakout | Pullback |
|---|---|---|---|---|
| Baseline (code cũ sau khi sửa gộp bar DNSE) | 30% OOS | 39 | n=18, thắng 11,1%, avg R −0,84 | n=21, thắng 38,1%, avg R −0,07 |
| Measured-move BẬT | 30% OOS | 284 | n=271, thắng 39,1%, avg R −0,08 | n=13, thắng 30,8%, avg R −0,23 |
| Measured-move BẬT | toàn chuỗi | 903 | n=875, thắng 36,3%, avg R −0,15 | n=28, thắng 35,7%, avg R −0,25 |
| **Chính sách ship** (measured-move TẮT) | toàn chuỗi | 110 | n=68, thắng 17,6% (CI95 10-28%), avg R −0,73 | n=42, thắng 38,1% (CI95 25-53%), avg R −0,23 |

Kết luận thẳng thắn:

- Cả hai setup đều có **kỳ vọng âm sau phí/thuế/slippage** trên 5 năm vừa qua
  (74/110 lệnh chạm stop). Dòng "Backtest lịch sử cho setup..." trong prompt giờ có
  số thật (n ≥ 30) để Manager/LLM khiêm tốn, KHÔNG phải bằng chứng policy có lãi.
- Measured-move cải thiện breakout (avg R −0,84 → −0,15) nhưng vẫn âm và nhân số
  lệnh BUY lên ~13 lần, nên để TẮT mặc định; bật lại chỉ sau khi hiệu chuẩn tham số
  (`stock/policy_params.json` + `apply_params()` cho grid offline).
- Trước đó file stats không tồn tại và OOS 30% chỉ cho 39 lệnh (< 30/setup) nên
  dòng thống kê chưa từng hiển thị. Hạn chế: universe hiện tại (survivorship),
  target khớp đúng giá target khi high chạm (lạc quan), không mô phỏng không
  khớp được ở giá sàn.
- Việc nên làm tiếp (offline, không phải Render): grid tham số `confidence_buy_min`,
  `min_rr_ratio`, `atr_stop_mult`, bộ lọc regime; bootstrap CI cho expectancy;
  so sánh với "mua và giữ VNINDEX" cùng kỳ.

Cách chạy lại (máy có mạng tới DNSE, KHÔNG phải Render):

```bash
python - <<'EOF'
import asyncio, json
from dataclasses import asdict
from stock import backtest, sector
results = asyncio.run(backtest.run_backtest(sorted(sector.ALL_KNOWN_SYMBOLS), full_sample=True))
backtest.save_setup_stats(results)
print(backtest.format_backtest_summary(results))
EOF
```

## Chưa làm (cố ý) và việc nên làm tiếp

- Không xây backtest/train trên Render (chủ repo yêu cầu; Render free không đủ
  tài nguyên). Stats sinh offline rồi commit.
- Trend model (`stock/trend_model.py`) giữ nguyên, vẫn tắt vì stats chưa vượt
  cổng; cân nhắc gỡ khỏi runtime nếu không có kế hoạch train lại.
- Chưa tách `stock/analysis.py` (1244 dòng) và `services/market_page.py`
  (1409 dòng) thành module nhỏ, chưa gom 10 file `.py` ở gốc vào package: diff
  quá lớn cho một đợt, nên làm riêng.
- Nhánh router9 native function-calling (`_ROUTER9_NATIVE_TOOLS_ENABLED=False`)
  giữ nguyên để bật lại khi gateway hỗ trợ ổn định.
- Lịch nghỉ Tết/Giỗ Tổ trong `stock/calendar.py` ghi tới 2026; mỗi năm thêm
  bảng mới (hoặc `STOCK_EXTRA_HOLIDAYS`) khi Sở công bố.
