# Kế hoạch tối ưu và nâng cấp toàn hệ thống (11/2026)

Kết quả rà soát toàn repo ngày 27/11/2026 trên commit `74f56b1` (bản 1.5.0). Phần 1-8 là
kế hoạch ban đầu; trạng thái thực hiện ở mục 0. Mỗi mục ghi file:dòng, vấn đề, cách sửa và
độ lớn (S = dưới nửa ngày, M = 1-2 ngày, L = 3 ngày trở lên).

## 0. Trạng thái thực hiện (27/11/2026, bản 1.6.0)

Đã làm toàn bộ 4 đợt trên nhánh `ccr-eaf8b8f9-jdsjbf`, cộng yêu cầu thêm của chủ bot cho
luồng Zalo → Facebook (hiện tên nhóm thay group ID, lọc trùng giữa các nhóm, tối ưu bộ lọc).
Chi tiết cho người dùng: mục 1.6.0 trong `CHANGELOG.md`.

| Chỉ số | Trước | Sau |
|---|---|---|
| Test (`pytest -q`) | 4.269 | ~4.580 |
| pyright | 0 lỗi, 887 cảnh báo | 0 lỗi, 383 cảnh báo (`core/` chặn truy cập None) |
| `/phantich` 1 mã | 5 lượt AI, 12 s nghỉ | 3 lượt, 0 s nghỉ; hỏi lại trong 15 phút: 1 lượt, 0 lần tải dữ liệu |
| So trùng 1 bài Facebook (800 bài cũ) | 63 ms, chặn event loop | 16 ms, trong thread |
| `/lich/le-hoi` truyền tải | 120 KB | 26 KB (xem lại: 304) |
| Admin tự làm mới | 12 request / 30 s | 1 request / 60 s |
| `core/database.py` | 2.190 dòng | 524 dòng |
| `services/nl_commands.py` | 1.705 dòng | gói 11 module |
| Vòng nền khi rảnh | inbox 2×5 s, outbox 15 s, nhắc 30 s, nhóm 60 s, tín hiệu 20 s | inbox 30 s, outbox 90 s + kick, nhắc ngủ tới giờ đến hạn (tối đa 120 s), nhóm 5 phút, tín hiệu theo nhịp cấu hình |

Làm một phần hoặc cố ý không làm:

- **Cần mạng nên chưa làm** (AGENTS.md: phải hỏi chủ repo): nâng `uvicorn` 0.29, nâng
  `zca-js` (đã có test bề mặt API + checklist trong `zalo-gateway/README.md`), tự host phông
  chữ web (đã làm cho phông không chặn hiển thị).
- **Không có gì để làm**: nén PNG không mất dữ liệu (ảnh đã tối ưu sẵn).
- **Chưa đạt mục tiêu**: `/lich/le-hoi` gzip còn 26 KB (mục tiêu < 20 KB) - muốn thấp hơn phải
  bớt/chuyển nội dung mô tả lễ hội, là quyết định nội dung.
- **Giữ lại có lý do**: bảng `telegram_processed_updates`, `zoom_processed_events` (migration
  008 và `backup.yml` còn dùng), `idx_user_facts_user` (còn truy vấn dùng); không tự xoá `notes`
  theo tuổi (dữ liệu người dùng).
- **AI**: dịch, `/gia`, bát tự vẫn đi chuỗi chat (đầu ra dài, không phải việc 1 dòng); JSON có
  schema mới áp cho router lệnh, chưa cho trích nhớ.
- **Lượt ảnh Zalo phía Python** vẫn giữ bytes ảnh tới hết lượt (lợi ích nhỏ, phải sửa nhiều
  module); phía gateway đã nhả sớm.
- **Tái cấu trúc**: lệnh nhóm Zalo và `/fb_*` chưa sinh từ `command_catalog`;
  `turn_control.is_readonly` còn danh sách lệnh chỉ đọc riêng.

Việc chủ bot cần làm khi deploy:

1. Migration 032, 033 tự chạy ở lần dùng DB đầu tiên.
2. Mọi phiên `/admin` bị đăng xuất 1 lần (khoá phiên mới dẫn xuất HKDF từ `ADMIN_PASS` +
   `SETTINGS_ENC_KEY`).
3. Nếu Render dashboard đặt tay `LIVE_CHAT_FALLBACK=1`, `ZALO_OUTBOX_POLL_MS=15000`,
   `ZALO_GROUP_REFRESH_MS=60000` hoặc `REMINDER_CHECK_INTERVAL_SEC=30` thì sửa theo
   `render.yaml`/`.env.example` (0 / 90000 / 300000 / 120) hoặc xoá để dùng mặc định.
4. Build lại APK qua GitHub Actions (máy làm việc không có Android SDK).
5. Gateway Zalo cần Node 22 (image Docker đã dùng Node 22) và mọi lệnh tới cổng điều khiển
   phải kèm `x-zalo-bridge-secret` (Python đã gửi kèm).

## 1. Hiện trạng đo được

| Chỉ số | Giá trị |
|---|---|
| `ruff check .` | sạch |
| `pyright` | 0 lỗi, 887 cảnh báo |
| `pytest -q` | 4.269 pass, 144 skip, 84 giây |
| RSS sau `import web` | 97,6 MiB (pandas không nạp) |
| `import web` | 1,24 giây (bot_app 0,65 s, channel_chat_service 0,26 s) |
| site-packages | 668 MB; scipy 109 + pandas 75 + sklearn 49 + numpy 43 + matplotlib 34 MB |
| Mã Python ngoài test | ~90.000 dòng; file lớn nhất `core/database.py` 2.190, `web_site.py` 2.008, `nl_commands.py` 1.705 |
| Biến môi trường | 299 dòng trong `.env.example` |

Những gì các đợt trước đã làm tốt và KHÔNG cần làm lại: cache có trần ở mọi module,
không vi phạm layering (services/ai/channels/core/stock không import handlers), inbox
lease + idempotency, prompt budget, cooldown theo Retry-After, retention 11 bảng, gateway
Zalo không có cấu trúc nào tăng không giới hạn, policy cổ phiếu không bị LLM đè số.

## 2. Nguyên tắc xếp ưu tiên

1. Lỗi làm mất tin, mất nhắc việc hoặc treo bot trên Render 512 MB đi trước.
2. Việc tiết kiệm quota AI miễn phí và số lượt DB Supabase đi thứ hai (đây là hai tài
   nguyên khan nhất).
3. Tái cấu trúc chỉ làm ở chỗ đã có hai bản sao logic hoặc file quá lớn đến mức mỗi
   tính năng mới phải sửa hai nơi.
4. Mỗi đợt là 1-3 PR nhỏ, mỗi PR có mục CHANGELOG, test khoá hành vi, chạy đủ bộ kiểm
   tra trong AGENTS.md. Không gộp đợt.

## 3. Đợt 1 - Sửa lỗi nguy hiểm và thắng nhanh (1-2 tuần, toàn S)

### 3.1 Độ bền (mất tin, mất nhắc)

- `scheduler.py:85` dựng `DueReminder(*item)` ngoài `try`, và `_reminder_loop` (:138)
  không bọc `_process_due_reminders`; một dòng dữ liệu lệch cột giết task nhắc việc vĩnh
  viễn tới khi redeploy. Sửa: đưa vào trong `try`, bọc thân vòng lặp như
  `_daily_digest_loop`, thêm kiểm tra task còn sống vào watchdog.
- `services/concurrency.py:122` `background_slot(OWNER_TURN_KEY)` không lấy chỗ thành
  viên, nên "việc nền của chủ bot + 1 lượt Zalo thành viên" chiếm hết 2 chỗ và lượt chat
  của chính chủ bot phải chờ tới 600 s. Sửa: việc nền luôn lấy `_member_slots`.
- `channels/router.py:290` lượt Zalo chờ khoá `assistant_turn` TRƯỚC khi `turn_deadline`
  bắt đầu, nên chờ + 200 s có thể vượt 240 s timeout của gateway; Python trả lời xong,
  cache rồi, nhưng không ai gửi, người dùng nhận câu lỗi. Sửa: sau lượt, nếu
  `request.is_disconnected()` thì `enqueue_outbox` để vòng outbox gửi bù (M, nhưng nên
  làm ngay).
- `zalo-gateway/src/index.ts` không bắt SIGTERM: supervisord dừng gateway trước web, tin
  đang gửi dở và tiến độ outbox mất, khi lên lại gửi lặp chunk 0. Sửa: handler SIGTERM
  dừng nhận, chờ hàng đợi tới 10 s, báo chunk đã gửi qua `/outbox/{id}/failed`.
- `zalo-gateway/src/index.ts:1061` bootstrap bỏ cuộc sau 30 lần (khoảng 4,5 phút Python
  chưa lên, vd Supabase thức chậm) mà chỉ `console.error`, không báo chủ bot, không thử
  lại. Sửa: gọi `reportState("login_required")` và giao cho `ReconnectScheduler`.
- `zalo-gateway/src/bridge.ts:189` nhận 503 + Retry-After từ Python (media worker bận) là
  lỗi cứng, trả câu lỗi ngay dù 5 s sau sẽ thành công và đã idempotent theo message_id.
  Sửa: retry tối đa 3 lần với 503/ECONNREFUSED.
- `core/database.py:105` `_reset_pool` gọi `pool.close()`; asyncpg chờ MỌI kết nối đang
  mượn trả về, nên một VACUUM FULL (tới 600 s) hoặc migration làm mọi lượt dính kết nối
  chết treo theo. Sửa: `pool.expire_connections()` hoặc `wait_for(close, 5)` rồi
  `terminate()`; áp cả cho `close_pool()` (:2113, supervisord chỉ chờ 40 s).
- `core/migrations.py:107` migration chạy với `command_timeout=30` và không có
  `lock_timeout`; DDL dài quá 30 s làm `init_db` ném lỗi, supervisord restart 20 lần và
  chạy lại cùng DDL. Sửa: `timeout=600`, `SET lock_timeout='10s'`, log version sắp áp.

### 3.2 Quota AI miễn phí

- `ai/orchestrator.py:1032` lượt chat qua Groq gửi nguyên persona 9-15k ký tự (3,5-6k
  token tiếng Việt) + lịch sử + `max_tokens=4096` vào model gpt-oss-20b trần 8K TPM; phần
  lớn lượt sẽ 413/429, tốn 1 request, log ERROR, rồi mới rơi sang OpenRouter. Sửa: persona
  rút gọn cho Groq (như `agent_service._persona`, ~1k ký tự) và `max_tokens=1024`; xác
  nhận bằng `provider_calls` stage=chat outcome.
- `ai/groq_client.py:48,84,129` không đặt `reasoning_effort`; gpt-oss mặc định "medium"
  đốt TPM và độ trễ cho chat thường và router JSON. Sửa: `GROQ_REASONING_EFFORT` mặc định
  "low" cho cả ba hàm.
- `ai/orchestrator.py:793` `ask()` không có `max_tokens`/`temperature`/json và chạy chuỗi
  đầy đủ từ router9 (timeout 120 s) cho việc 1 dòng: viết lại query tìm kiếm
  (`web_search.py:226`, chạy mỗi lượt tìm), router dự phòng, dịch, giá sản phẩm, bát tự.
  Sửa: thêm kwargs và chuyển các việc nhỏ qua `utility_json` hoặc
  `providers_override=["groq","api1","api2"]`.
- `ai/prompt_budget.py:17` `CHARS_PER_TOKEN=4` lạc quan cho tiếng Việt có dấu (thực tế
  2,5-3); ngân sách 40k ký tự là 13-16k token chứ không phải 10k. Sửa: 2,8 và thêm test
  hiệu chỉnh từ `prompt_chars`/`prompt_tokens` đã ghi trong `provider_calls`.
- `ai/provider_state.py:34` thiếu `groq_stt`, `tts_api1/2` trong danh sách cooldown nên
  sau restart Render các cooldown này không được nạp lại. Sửa: thêm key.
- `ai/orchestrator.py:775` lần thử cuối gọi lại router9 kể cả khi nó chết vì 401/403.
  Sửa: bỏ qua khi lý do là auth.

### 3.3 Số lượt DB Supabase khi rảnh

- `core/database.py:266` `statement_cache_size=0` với lý do "transaction mode" nhưng README
  bắt buộc Session Pooler (migration cần advisory lock); mỗi query mất 2 vòng thay vì 1,
  khoảng 10 query mỗi lượt chat. Sửa: cache mặc định khi cổng 5432, giữ 0 chỉ khi 6543.
- `core/webhook_inbox.py:359` hai worker `claim()` mỗi 5 s khi rảnh (~35k lệnh/ngày) dù
  `enqueue` đã có `_wake`. Sửa: chờ 30-60 s, dời `_wake.clear()` lên trước `claim()`.
- `zalo-gateway/src/index.ts:1146` poll outbox mỗi 15 s (5.760 SELECT/ngày) và
  refreshGroups 2 GET/phút khi rảnh. Sửa: Python "kick" `/outbox/kick` lúc enqueue (đã có
  mẫu `/groups/refresh`), gateway poll 60-120 s dự phòng.
- `services/zalo_signals.py:695` ngủ 20 s rồi đọc setting để biết nhịp quét; ngủ đúng
  `interval*60`.
- `ai/orchestrator.py:517,610` mỗi lượt chờ INSERT `provider_calls` + persist provider
  đang dùng trước khi trả lời. Sửa: fire-and-forget vào tập task có trần (mẫu
  `core.alerts.tasks`).
- Migration 032: index `results(prompt_id)` (cascade delete mỗi lần prune prompt),
  `chat_messages(created_at)` (sweep trí nhớ mỗi 600 s đang seq scan),
  `bg_jobs(routine_id, id DESC)`; bỏ `idx_reminders_due` (tiền tố của
  `idx_reminders_claimable`), `idx_prompts_channel` (không query), hai bảng
  `telegram_processed_updates`/`zoom_processed_events` không còn code dùng từ 015.
- Retention: `notes` chưa có trần, `app_devices` thu hồi giữ mãi, `backup_runs` mỗi
  ngày 1 dòng. Thêm 3 rule vào `services/db_maintenance.py:69`.

### 3.4 Web công khai

- `web.py:208` chưa có nén: `site.css` 64 KB, `/lich/le-hoi` 120 KB HTML, `/admin` 110 KB
  gửi thô. Sửa: `GZipMiddleware(minimum_size=1024)` và bỏ gzip tự viết trong
  `web_app_api.py:39`.
- `web_site.py:450` `_render` không có ETag/Cache-Control cho trang chỉ phụ thuộc URL
  (`/lich?ngay=`, `/tuoi/{y}`, `/lich/le-hoi?nam=`, API calendar). Sửa: ETag yếu + 304,
  `public, max-age=3600`, `Vary: Cookie`; giữ no-cache cho trang "hôm nay".
- `templates/admin.html:430` tự làm mới 30 s bắn 12 request song song, trong đó
  `db-usage` chạy `sum(pg_column_size(content))` toàn bảng; 1 tab admin chiếm 12/32 chỗ
  uvicorn mỗi 30 s. Sửa: 1 endpoint `/admin/api/overview` có cache TTL phía server
  (db-usage/watchdog 5 phút, usage 60 s), mặc định 60 s (M).
- `web_admin.py:245` khoá HMAC phiên là chính `ADMIN_PASS`; cookie lộ cho phép dò mật
  khẩu offline. Sửa: HKDF từ `ADMIN_PASS` + `SETTINGS_ENC_KEY`.
- `web_admin.py:734` rào CSRF Content-Type chỉ áp cho `/journal/note`; các POST đổi API
  key, reset cooldown, chạy market_page, xoá trí nhớ chỉ dựa vào SameSite=Lax. Sửa: đưa
  rào thành dependency của `api_router` + từ chối `Sec-Fetch-Site: cross-site`.
- `templates/site/base.html:36` Google Fonts chặn render và thêm 1 DNS/TLS bên thứ ba.
  Sửa: self-host woff2 subset, `font-display: swap`.

### 3.5 Dependency và build

- `requirements.txt` ghim pandas, numpy, matplotlib, mplfinance, seaborn, openpyxl,
  cycler, tenacity, pytz, cffi, packaging, importlib-metadata, psutil "vì app còn dùng"
  nhưng không module nào ngoài `scripts/` import chúng (đã grep). Gỡ, chạy `pytest` +
  `pip check`, chuyển numpy sang requirements-dev cho 2 script. Image Render nhẹ đi vài
  trăm MB, deploy nhanh hơn, pip-audit bớt bề mặt.
- `uvicorn==0.29.0` (3/2024) trong khi fastapi 0.143 / starlette 1.10 mới; nâng lên bản
  2026 và `pip check`.
- `Dockerfile:17` `PYTHONDONTWRITEBYTECODE=1` không có `compileall`: mỗi lần supervisord
  restart biên dịch lại ~300 module. Sửa: `RUN python -m compileall -q /app`.
- `zalo-gateway/package.json` `engines ">=18"` trong khi build và code theo Node 22; sửa
  `>=22`, thêm `.nvmrc`.

## 4. Đợt 2 - Hiệu năng và chi phí theo tính năng (2-3 tuần)

### 4.1 Phân tích cổ phiếu (`/phantich`)

- `stock/analysis.py:1302,1394` cache 15 phút đang chết: cả hai nơi gọi đều truyền
  `user_text` nên không đọc/ghi cache; hỏi "phân tích HPG" hai lần trong 5 phút chạy lại
  7+ lượt mạng và tới 5 lượt LLM. Sửa: cache `StockContext` + 4 khối debate theo
  (symbol, holding, full_access), chỉ chạy lại bước tổng hợp với câu hỏi người dùng (M).
- `stock/debate.py:179` 4 lượt JSON tuần tự + tổng hợp, nghỉ 3 s giữa mỗi bước (12 s ngủ
  thuần), mỗi bước retry 1 lần: xấu nhất 9 lượt LLM trong hạn 200 s. Sửa: gộp bull+bear
  vào 1 lượt với schema `{bull:{}, bear:{}}`, gộp Manager vào tổng hợp: 5 lượt còn 3,
  bỏ 2 lần ngủ (M).
- `stock/schemas.py:114` bước có cấu trúc đi qua `ask()` chữ thường + regex JSON + retry
  pydantic trong khi `utility_json()` đã có Groq json_object + Gemini dự phòng. Chuyển qua
  `utility_json` (S).
- `stock/price_adjust.py:30` ngưỡng gap "nghi ngờ" 12 % cố định dù UPCOM biên độ ±15 %
  (mã mới ±20-30 %): một phiên trần/sàn hợp lệ của UPCOM bị coi là chưa điều chỉnh, policy
  trả HOLD/NO_TRADE tin cậy 0 trong ~3 tháng. Sửa: truyền `exchange`, ngưỡng = biên độ ×
  1,3; thêm test UPCOM (S). Cùng lỗi ở `stock/policy.py:49` ngưỡng sát trần 5,5 %/gần sàn
  -6 % là số HOSE áp cho HNX/UPCOM.
- `stock/vci_direct.py:102` mỗi lần gọi `requests.get` mới (TLS mới, ~13 lượt mỗi
  `/phantich` lạnh), không retry 429/5xx; `_ratio_cache` giữ nguyên hàng VCI 41 quý × ~150
  trường cho 128 mã dù chỉ đọc 8 trường. Sửa: 1 `httpx.Client` dùng chung (bỏ luôn
  `requests`), retry 1 lần, chiếu hàng về đúng key trước khi cache (S).
- `stock/providers.py:107` `fetch_ohlcv` không gộp request đang bay (danh mục N mã bắn N
  lần VNINDEX cùng lúc), và cùng mã lấy 260/90/5 phiên thành 2-3 lượt DNSE. Sửa: Future
  theo (sym, days) và cache theo chuỗi dài nhất rồi cắt (S).
- `stock/fundamentals.py:358` bundle toàn None (VCI lỗi thoáng) bị cache 30 phút. TTL
  2-3 phút khi rỗng (S). `stock/sector.py:155` context ngành cache 180 s dù đo xu hướng
  1-3 tháng: 15-30 phút (S). Google News RSS (`providers.py:684`) chưa cache: 10 phút (S).
- `handlers/stock_handler.py:86` "so sánh A B C" chạy đủ pipeline tuần tự từng mã; sửa:
  gather `build_context` cho mọi mã trước, tối đa 2 mã debate đầy đủ, còn lại 1 lượt tổng
  hợp (M).
- `stock/trend_model.py:430` đường ML chết theo thiết kế (stats thiếu split_method, không
  có joblib trên Render); gỡ nhánh runtime, giữ script train (S).

### 4.2 Lượt chat

- `core/database.py:771` `get_session_messages` đọc `max(k*4, 40)` dòng nguyên `content`
  mỗi lượt chỉ để tìm khoảng nghỉ phiên bằng Python rồi giữ 20. Sửa: tính khoảng nghỉ
  bằng `LAG()` trong SQL, `left(content, n)`, đúng k dòng (S).
- `services/memory_service.py:906` `maybe_flush` lấy semaphore `_slots` trước khi kiểm
  tra rẻ `_should_flush`, và `_should_flush` + `flush` mỗi hàm tự đọc 60 dòng chat. Sửa:
  kiểm tra ngoài semaphore, truyền dòng đã đọc vào `flush` (S).
- `ai/openai_compatible.py:409` chunk SSE `{"error":...}` sau khi đã có chữ bị nuốt,
  người dùng nhận câu bị cụt không báo. Sửa: raise hoặc đánh dấu truncated để chuỗi thử
  provider khác (S). Cùng file :561 nối lại toàn bộ pieces mỗi delta (O(n²)): giữ chuỗi
  chạy (S).
- `ai/agent_service.py:557` tới bước cuối model vẫn xin tool thì vứt hết kết quả tool và
  trả câu "quá bước". Sửa: bước cuối gửi `tool_choice="none"` / Gemini mode NONE (S).
- `core/memtrim.py:102` `gc.collect()` đồng bộ trên event loop sau mỗi lượt và mỗi 10
  phút, quét cả heap khởi động. Sửa: `gc.freeze()` cuối `_post_init`, log khi > 100 ms (S).
- `services/web_search.py` và `ai/orchestrator.py:168` lọc chú thích "[1]" chỉ chạy
  trong `chat()`; các nơi gọi `ask(require_real_search)` còn rác. Áp trong chuỗi cho
  stage "search" (S).

### 4.3 Facebook intake và bộ nhớ gateway

- `channels/router.py:606` handler `/facebook-group-post` làm nén ảnh, dHash, dedup,
  `prepare_post` (gọi AI viết lại đồng bộ) ngay trong request, giữ ngân sách "heavy" của
  RequestLimits nhiều phút (chặn `/voice`, `/image-prompt` 503) và chặn groupQueue của
  gateway tới 240 s. Sửa: chuyển vào `services/facebook_intake.py`, trả 204 ngay sau
  `create_post`, viết lại/preview/prune chạy nền với semaphore 1 (M). `prune_history()`
  (3 DELETE) chạy mỗi bài: giới hạn 1 lần/giờ (S).
- `zalo-gateway/src/index.ts:862` `seen.remember()` cho mọi tin của mọi nhóm kể cả nhóm
  không theo dõi, làm trôi LRU 5.000 id của tin riêng. Sửa: kiểm tra nhóm trước (S).
- `zalo-gateway/src/bridge.ts:358` media Facebook đi qua loopback dạng base64 trong JSON
  (~2,7 lần bản sao trên heap 96 MB). Sửa: multipart hoặc raw bytes như `/image-prompt`
  (M).
- Ảnh 8 MB được giữ suốt lượt (tới 240 s) ở cả hai phía (`index.ts:602`,
  `router.py:516`); giải phóng ngay sau khi gọi (S).

## 5. Đợt 3 - Tái cấu trúc có chủ đích (3-4 tuần)

Chỉ làm ở chỗ đã có hai bản sao hoặc file không còn đọc nổi; mỗi mục là 1 PR cơ học,
không đổi hành vi, test hiện có phải xanh nguyên.

- **Một đường "trả lời nhanh" cho mọi kênh.** `handlers/chat_router.py:134` `_quick_reply`
  viết tay lại 13 bước tiền-AI của `services/channel_chat_service.py:681`; hai bản đã
  lệch (Telegram gọi `attention.build`, Zalo gọi `attention_text`; Telegram thiếu
  `is_admin` khi gọi `routines`). Tách `services/quick_reply.py` với danh sách cổng có
  thứ tự, cả hai kênh gọi chung (M).
- **Một bảng lệnh cho ba kênh.** `bot_app.py:296` bảng 115 handler PTB,
  `services/channel_command_service.py:514` chuỗi if 25 nhánh và `services/command_catalog.py`
  mô tả cùng một bộ lệnh ở ba nơi. Sinh bảng PTB và dispatcher Zalo từ một danh sách
  catalog (M).
- **`core/database.py` 2.190 dòng → ~400.** Tách `core/db/pool.py` (dòng 1-270: pool,
  breaker, `with_reconnect`, `close_pool`); chuyển 21 hàm còn SQL inline (prompts/results,
  chat, facts/highlights/summary, notes, settings trùng `repositories/settings.py`, zoom
  pairing) vào `core/repositories/*`; thay 157 hàm bọc một dòng bằng helper
  `_bind(repo_fn, idempotent=...)` để mọi chỗ gọi `db.xxx` giữ nguyên. Làm theo từng
  domain, mỗi domain 1 PR (L). Kèm: `channels/facebook_repository.py` (27 chỗ `get_pool()`,
  0 `with_reconnect`), `zalo_repository.py` (16/19 hàm chưa bọc), `zalo_signal_repository.py`,
  `zalo_session.py` và `services/db_maintenance.py:208,236,309` chưa qua `with_reconnect`
  nên query đầu sau khi Supavisor cắt kết nối rảnh lỗi thẳng (M).
- **`services/nl_commands.py` 1.705 dòng → package** `services/nl_commands/{registry,
  builders_huyenhoc, builders_personal, builders_settings, codepath, help}.py` với
  `__init__` re-export (M). **`services/memory_service.py` 1.470 dòng** tách
  `memory_extract.py` (trích/flush/consolidate/sweep) và `memory_context.py` (dựng ngữ
  cảnh mỗi lượt), giữ facade (M).
- **Ba client provider trùng nhau.** `groq_client`, `openrouter_client`,
  `router9_client` lặp ~18 hàm gần giống (840 dòng). Một
  `OpenAICompatibleProvider(label, base_url, key, default_model, extra_payload,
  error_cls)` trong `openai_compatible.py`, giữ alias module cho test và web_admin (M).
- **Gateway `index.ts` 1.155 dòng không có test** (mọi module khác đều có `test-*.mjs`).
  Tách `dispatcher.ts` (routeDirect, handleGroupMessage, handlers) nhận `{api, bridge,
  queues}` tiêm vào, test bằng fake; thêm test clamp cho `config.ts` (M). Thêm smoke test
  khẳng định các method zca-js đang dùng còn tồn tại và enum khớp shim, kèm checklist nâng
  zca-js (S).
- Lịch trình nền: `scheduler.py:138` ngủ tới `due_at` kế tiếp và được `reminder_service.create`
  đánh thức (mẫu Event của background_jobs) thay vì poll 30 s (M).

## 6. Đợt 4 - Nâng cấp nền tảng (sau khi đợt 1-3 xong)

- **Đầu ra có cấu trúc thật**: `ai/official_client.py:498` và `groq_client.py:84` dùng
  JSON mode không schema; chuyển sang `response_json_schema` (google-genai 2.11) và Groq
  `json_schema` sinh từ `tool_registry` cho router, memory ops, debate cổ phiếu (M).
  Kèm `ThinkingConfig(thinking_budget=0)` cho việc tiện ích trên Gemini flash-lite (S).
- **Quan sát pool DB**: ghi `pool.get_size()/get_idle_size()` và thời gian chờ acquire
  vào `turn_metrics` và `/status`; sau 1-2 tuần số liệu mới quyết `max_size` 5 → 8 và
  `min_size` 2 (tránh TLS lại sau mỗi 60 s rảnh) (S).
- **httpx**: mọi client có `Limits(max_connections=4, max_keepalive=2)`; các chỗ tạo
  client mỗi lần gọi (`price_search.py:86`, `rag_clean_service.py:314`,
  `zalo_gateway_status.py`, `handlers/zalo_login.py:13`) đi qua `http_client.scoped()`;
  Tavily/Agnes dùng client chung (S).
- **Admin/API**: `Cache-Control: no-store` cho `/admin/api/*`; cache âm + giới hạn theo IP
  cho token app sai (`services/app_devices.py:126`); ETag cho dashboard app; Android ép
  https và `usesCleartextTraffic=false` (S mỗi mục).
- **Logging**: `_RedactFilter` dùng 1 regex gộp thay ~20 `replace`; handler qua
  `QueueHandler/QueueListener` để không ghi stdout đồng bộ trên event loop;
  `voice_service.py:181` không log 80 ký tự lời nói người dùng (S).
- **Bảo mật thêm**: control server gateway (`control-server.ts:91`) yêu cầu cùng secret
  như Python; `channels/zoom.py:87` so sánh token theo bytes (header non-ASCII hiện ném
  500 thay vì 403); `Permissions-Policy` trong `security_headers.py` (S).
- **CI**: thêm kịch bản "1 lượt chat với provider giả" vào `scripts/benchmark_memory.py`
  và dump `-X importtime` top 20 để bắt hồi quy RAM/khởi động; nâng pyright từ 887 cảnh
  báo theo từng package (bật `reportOptionalMemberAccess` = error cho `core/` trước) (M).
- **Tài liệu**: README 215 KB một file; tách theo chủ đề (`docs/telegram.md`,
  `docs/zalo.md`, `docs/stock.md`, `docs/web.md`, `docs/van-hanh.md`) và để README còn
  tổng quan + mục lục; `.env.example` 299 biến gom theo nhóm có "bắt buộc/tuỳ chọn" (M).

## 7. Việc chủ bot cần quyết trước khi làm

1. Cổng Supabase đang dùng trên Render là 5432 (session) hay 6543 (transaction)? Quyết
   định `statement_cache_size` và `max_size` phụ thuộc câu này.
2. Có chấp nhận gộp bull+bear vào 1 lượt LLM (báo cáo ngắn hơn một chút, nhanh hơn
   ~40 %, ít tốn quota hơn ~40 %) không? Nếu không, vẫn làm được cache context (4.1 mục 1).
3. `LIVE_CHAT_FALLBACK` (core/config.py:315) mặc định bật: lượt chữ cuối cùng mở WebSocket
   âm thanh Gemini Live, chậm và tốn RAM nhất. Đề nghị mặc định tắt.
4. Gỡ 12 package Python không dùng có cần giữ lại cái nào cho script offline không
   (numpy cho 2 script tạo ảnh và benchmark).
5. Thứ tự ưu tiên giữa đợt 2 (cổ phiếu, chat) và đợt 3 (tái cấu trúc) nếu chỉ có thời
   gian cho một trong hai trước Tết.

## 8. Số đo mục tiêu để biết đã xong

| Chỉ số | Hiện tại | Mục tiêu sau đợt 1-2 |
|---|---|---|
| Lượt DB/phút khi rảnh | ~30 | < 8 |
| Round-trip DB mỗi lượt chat | ~20 (cache=0) | ~10 |
| `/phantich` cùng mã lần 2 trong 15 phút | 7+ fetch, 5 LLM, 12 s ngủ | 0 fetch, 1 LLM |
| `/phantich` lạnh | tới 9 LLM, 200 s | tối đa 4 LLM, < 90 s |
| Tỉ lệ Groq chat 413/429 (`provider_calls`) | đo trước | < 5 % |
| `/lich/le-hoi` truyền tải | 120 KB | < 20 KB (gzip) + 304 |
| Kích thước image Render | đo lúc build | giảm ≥ 300 MB |
| Nhắc việc sống sót dòng lỗi | không | có (test + watchdog) |
| `core/database.py` | 2.190 dòng | < 500 dòng |

Cách kiểm chứng mỗi PR: `ruff check .`, `pytest -q`, gateway `npm run check && npm test`,
`python scripts/benchmark_memory.py`, và với PR DB: `python -m core.migrations --write-lock`.
