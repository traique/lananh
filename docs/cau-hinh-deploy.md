# Cấu hình, chạy local và deploy Render

Biến môi trường bắt buộc và tuỳ chọn, chạy local, deploy Render và giữ Render Free không ngủ.

Tài liệu chính: [README.md](../README.md).

## Cấu hình bắt buộc

| Biến | Mục đích |
|---|---|
| `TELEGRAM_TOKEN` | Token từ BotFather |
| `ALLOWED_USER_ID` | Telegram user duy nhất được phép dùng bot |
| `ROUTER9_API_KEY` | API key 9Router (provider AI đầu tiên trong chain) |
| `DATABASE_URL` | Supabase Session Pooler URL |
| `SETTINGS_ENC_KEY` | Fernet key mã hóa settings nhạy cảm |
| `WEBHOOK_SECRET` | Secret cho Telegram webhook trên Render |
| `WEBHOOK_BASE_URL` | Public base URL; Render có thể cung cấp `RENDER_EXTERNAL_URL` |

Tạo secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`SETTINGS_ENC_KEY` là bắt buộc và fail closed. Không đổi hoặc làm mất key sau khi đã lưu ciphertext.

Xem `.env.example` và `render.yaml` để biết toàn bộ biến tùy chọn. Các biến về độ
bền/bảo mật (đều có mặc định an toàn, chỉ đặt khi cần đổi):

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `CHAT_TURN_TIMEOUT_SEC` | 200 | Hạn chót tổng cho 1 lượt trả lời (30-230) |
| `MEMORY_UPDATE_TIMEOUT_SEC` / `RAG_CLEAN_TOTAL_TIMEOUT_SEC` | 90 / 900 | Trần 1 lượt trích trí nhớ nền (theo lô); trần TỔNG 1 lượt `/ragxuly` (chạy nền ngoài khoá chat) |
| `PROMPT_BUDGET_CHARS` / `HISTORY_MESSAGE_MAX_CHARS` / `GROUNDING_MAX_CHARS` / `MEMORY_MAX_CHARS` | 28000 / 1500 / 9000 / 3000 | Ngân sách ký tự prompt gửi provider (~2,8 ký tự/token tiếng Việt: 28000 ≈ 10k token) và trần từng khối |
| `PERSONA_STOCK_SPLIT` | 1 | Lượt chat không liên quan cổ phiếu/đầu tư chỉ gửi phần lõi của persona; 0 = luôn gửi cả phần phân tích cổ phiếu |
| `DATABASE_SSL_MODE` / `DATABASE_SSL_CA` | `require` / (trống) | `verify-full` xác thực chứng chỉ + hostname Postgres (cần CA Supabase); `disable` chỉ cho DB local |
| `SETTINGS_ENC_KEY_OLD` / `SETTINGS_REJECT_PLAINTEXT` | (trống) / 0 | Khoá Fernet cũ khi xoay khoá (MultiFernet); từ chối giá trị settings chưa mã hoá |
| `ENABLE_PORTFOLIO_ALERTS` | true | Cảnh báo danh mục cuối phiên (chạm stop/target, breakout) |
| `FACEBOOK_MAX_MEDIA_MB` | 150 | Trần TỔNG ảnh Facebook đang lưu/tài khoản (xoá bài chờ cũ nhất, rồi lưu bài không ảnh) |
| `STOCK_EXTRA_HOLIDAYS` / `STOCK_POLICY_PARAMS_PATH` | (trống) | Ngày nghỉ giao dịch bổ sung; file tham số policy thay `stock/policy_params.json` |
| `GROQ_TOTAL_TIMEOUT_SEC` / `OPENROUTER_TOTAL_TIMEOUT_SEC` | 90 | Trần tổng 1 lượt gọi provider (kể cả stream nhỏ giọt) |
| `WEB_READER_MAX_BYTES` | 2097152 | Trần byte tải về cho 1 trang/feed |
| `WEBHOOK_MAX_ATTEMPTS` / `WEBHOOK_DEAD_RETENTION_DAYS` | 8 / 14 | Số lần thử sự kiện webhook trước khi DEAD; số ngày giữ dòng DEAD |
| `RETENTION_USER_CALLS_DAYS` / `RETENTION_PROVIDER_CALLS_DAYS` | 180 / 180 | Dọn thống kê cũ hằng ngày (0 = tắt) |
| `RETENTION_ZALO_OUTBOX_DAYS` / `RETENTION_REMINDERS_DAYS` | 30 / 90 | Dọn outbox đã gửi/bỏ cuộc và nhắc việc đã gửi |
| `REQUEST_HEAVY_BUDGET_MB` | 30 | Tổng MB body lớn (ảnh, bài Facebook) được xử lý cùng lúc |
| `ADMIN_TRUSTED_PROXY_HOPS` | 1 | Số proxy tin cậy nối IP vào `X-Forwarded-For` (Render = 1) |
| `ADMIN_CLIENT_IP_HEADER` | (trống) | Header IP do proxy tin cậy ghi đè (vd `true-client-ip`) - chỉ đặt khi đã kiểm tra |
| `ZALO_OUTBOX_MAX_ATTEMPTS` | 5 | Số lần thử 1 tin outbox Zalo trước khi bỏ cuộc |
| `ZALO_DIRECT_CONCURRENCY` / `ZALO_DIRECT_SENDER_BACKLOG` | 1 / 8 | Gateway: số tin riêng xử lý song song (= `MAX_CONCURRENT_TURNS - OWNER_RESERVED_SLOTS`); số tin chờ tối đa mỗi người |
| `ZALO_BRIDGE_ALLOWED_HOSTS` / `ZALO_BRIDGE_ALLOW_FORWARDED` | `127.0.0.1,::1` / 0 | Ai được gọi `/internal/zalo/*`; đổi khi gateway chạy ở máy khác |

### Biến môi trường mới (đợt 26/10/2026)

Đều có mặc định an toàn (đã có trong `.env.example`; biến không phải secret có
trong `render.yaml`). `test/test_env_example.py` đỏ khi code đọc biến mới mà
`.env.example` chưa có.

Provider, tra web, quota:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `GROQ_REALTIME_MODEL` | `openai/gpt-oss-120b` | Model Groq tự tra web (`browser_search`, chỉ `openai/gpt-oss-*`); giá trị `groq/compound(-mini)` cũ tự đổi về mặc định |
| `GROQ_WEB_SEARCH` | 1 | Cho Groq tự tra web ở lượt `native`/`forced` |
| `OPENROUTER_WEB_SEARCH` | 0 | Cho OpenRouter tự tra web (plugin `web`, tốn credit kể cả model `:free`) |
| `ROUTER9_WEB_SEARCH` / `ROUTER9_WEB_SEARCH_MODEL` | 0 / (trống = model chat) | Cho 9Router tự tra web qua `web_search_options`, chỉ tin khi có `url_citation` |
| `REPLY_URL_FILTER` | `untrusted` | Vô hiệu link lạ trong câu trả lời: `untrusted` / `all` / `off` |
| `API_QUOTA_COOLDOWN_SEC` | 3600 | Nay chỉ là cooldown cho hết quota THEO NGÀY; 429 theo phút dùng gợi ý của provider (30 giây-15 phút) |
| `GROQ_HISTORY_MAX_CHARS` | 3500 | Trần ký tự lịch sử riêng cho Groq (gpt-oss-20b free khoảng 8K token/phút) |
| `GROQ_TPM_BUDGET` | 7000 | Ngân sách token ước lượng cho 1 lượt Groq (prompt + max_tokens); quá thì persona gọn, vẫn quá thì bỏ qua Groq không tốn request. 0 = tắt |
| `GROQ_REASONING_EFFORT` | low | Mức suy luận gửi cho model `openai/gpt-oss-*` (low/medium/high) |
| `CHAT_TEMPERATURE` | 0.8 | Nhiệt độ chat thường (trước 0,95 - cao làm lẫn chữ Trung/Nhật/Hàn nhiều hơn) |
| `GEMINI_CHAT_THINKING_BUDGET` | (trống) | Ngân sách "thinking" của Gemini khi chat; trống = mặc định model. Việc JSON/tiện ích luôn 0 |
| `GROQ_STREAM_USAGE` | 1 | Xin token trong stream Groq (`stream_options.include_usage`) cho thống kê; 0 nếu Groq từ chối |
| `QUOTA_BURN_ALERT_PCT` | 80 | Báo chủ bot khi provider dùng từ N% hạn mức trong cửa sổ quota (0 = tắt) |
| `GEMINI_KEYS_SAME_PROJECT` | 0 | 1 = API 1 và API 2 cùng project Google Cloud (cộng chung quota) |
| `FREE_TIER_RPD_GEMINI` / `FREE_TIER_RPD_GROQ` | 0 / 0 | Hạn mức lượt/ngày (0 = chưa đặt, chỉ hiển thị); Groq ưu tiên số từ header response |
| `FREE_TIER_RPD_OPENROUTER` / `FREE_TIER_TAVILY_MONTHLY` | 50 / 1000 | OpenRouter `:free` 50 lượt/ngày (1000 khi đã nạp từ 10 USD); Tavily credit/tháng (tìm advanced = 2) |
| `VISION_PROVIDER_ORDER` | `api1,api2,openrouter,groq,router9` | Thứ tự provider cho ảnh (prompt từ ảnh, hỏi đáp ảnh, hoá đơn); provider thiếu tự thêm vào cuối |
| `LIVE_CHAT_FALLBACK` | 0 | 1 = Gemini Live làm provider chat dự phòng cuối (thêm `live` vào cuối `PROVIDER_ORDER`); mặc định tắt (đường chậm, tốn RAM nhất) |
| `LIVE_MODEL` | `gemini-3.8-live` | Model Gemini Live (đổi được ở `/admin` → Provider → `live`) |
| `LIVE_MAX_SESSIONS` / `LIVE_CALL_TIMEOUT_SEC` / `LIVE_MAX_AUDIO_MB` | 1 / 60 / 8 | Số phiên Live cùng lúc (1-4), trần giây 1 lượt (10-180, không vượt hạn chót lượt), trần MB âm thanh 1 lượt (1-32) |
| `LIVE_HISTORY_MESSAGES` | 6 | Số tin lịch sử gần nhất gửi kèm lượt chat qua Live (0-20) |
| `FREE_TIER_RPD_GEMINI_LIVE` | 0 | Hạn mức lượt/ngày của Live (live_api1 + live_api2); Google chưa công bố - để 0 (chỉ đếm) tới khi thấy số thật ở aistudio.google.com/rate-limit |

DB, bảo mật, lượt song song:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `DB_CONNECT_TIMEOUT_SEC` | 8 | Trần thời gian mở 1 kết nối DB (2-60) |
| `DB_BREAKER_OPEN_SEC` | 30 | Số giây báo lỗi nhanh khi DB vẫn mất sau reconnect (0 = tắt breaker) |
| `DB_POOL_MAX_SIZE` / `DB_POOL_MIN_SIZE` | 8 / 2 | Số kết nối tối đa/mở sẵn của pool asyncpg (Session Pooler free cho 15; kết nối rảnh quá 60 s vẫn bị đóng). `/status` có dòng "DB pool" (đang dùng, số lần chờ > 1 s) để biết khi nào cần tăng |
| `DB_STATEMENT_CACHE_SIZE` | (trống) | Cache prepared statement mỗi kết nối. Trống = tự chọn theo cổng `DATABASE_URL`: 6543 (transaction pooler) -> 0, cổng khác (Session Pooler 5432) -> 100, mỗi câu lệnh 1 vòng mạng thay vì 2 |
| `WEBHOOK_INBOX_IDLE_SEC` | 30 | Worker inbox webhook ngủ tối đa chừng này khi rảnh (tin mới đánh thức ngay) |
| `RETENTION_BACKUP_RUNS_DAYS` / `RETENTION_APP_DEVICES_DAYS` | 400 / 90 | Dọn nhịp tim sao lưu cũ và thiết bị app đã thu hồi / mã ghép hết hạn |
| `SECURITY_HSTS` | 1 | Header HSTS (không preload) cho web và `/admin`; 0 khi tên miền riêng chưa có HTTPS |
| `SETTINGS_AUTO_ROTATE` | 1 | Có `SETTINGS_ENC_KEY_OLD` thì lúc khởi động tự mã hoá lại giá trị còn ở khoá cũ; 0 = chỉ báo |
| `MAX_CONCURRENT_TURNS` | 2 | Tổng lượt AI song song mọi kênh |
| `OWNER_RESERVED_SLOTS` | 1 | Số chỗ trong `MAX_CONCURRENT_TURNS` luôn dành cho chủ bot (Telegram/Zoom/web); 0 = tắt, tự tắt khi MAX = 1 |
| `ZALO_DIRECT_CONCURRENCY` | 1 | Gateway: số người gửi Zalo xử lý song song, phải bằng số chỗ thành viên (MAX - dành riêng) |
| `ZALO_MEMBER_DAILY_TURNS` / `ZALO_MEMBER_DAILY_HEAVY` | 40 / 5 | Trần lượt / lượt nặng mỗi ngày của thành viên Zalo (role user), 0 = không giới hạn, sửa được trên `/admin` |
| `ZALO_WELCOME_ON_PAIR` | 1 | Lần đầu `/zalopair`/`/zaloadmin` gửi 1 lời chào qua outbox Zalo; 0 = không gửi |

Hội thoại, trí nhớ, hỏi thăm:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `CHAT_HISTORY_TURNS` | 10 | Nay tính theo LƯỢT hỏi-đáp (2 dòng/lượt); trước đây tính theo dòng |
| `CHAT_MESSAGES_RETENTION_LIMIT` | 1000 | Số dòng lịch sử giữ mỗi người (200-2000; trước đây cố định 200) |
| `MEMORY_FLUSH_EXCHANGES` | 3 | Trích nhớ khi có từng này lượt có nghĩa chưa xử lý |
| `MEMORY_IDLE_FLUSH_SEC` / `MEMORY_SWEEP_SEC` | 1800 / 600 | Rảnh bao lâu thì trích; chu kỳ vòng quét nền (chạy cả lúc khởi động) |
| `MEMORY_SWEEP_ENABLED` / `MEMORY_CONSOLIDATION` | 1 / 1 | Bật vòng quét nền; gộp fact trùng tối đa 1 lần/ngày khi gần đầy |
| `STYLE_RULES_NEED_APPROVAL` | 1 | Cách trả lời bộ trích nhớ tự rút ra chờ người dùng duyệt (`/hoc`, nút [Ghi nhớ] [Bỏ]); 0 = áp ngay như cũ |
| `PROMPT_REDACTION` | 1 | Che OTP/mật khẩu/STK/CCCD/số thẻ/API key trong prompt trước khi gửi provider; 0 = tắt |
| `CHAT_READ_LINKS` / `CHAT_READ_LINKS_MEMBERS` | 1 / 0 | Đọc link gửi kèm câu hỏi (chủ bot, admin); 1 = mở cho cả thành viên Zalo |
| `CHAT_AUTO_RAG` | 1 | Chat thường tự tra kho `rag/` khi khớp rất rõ (chủ bot/admin, không tốn AI) |
| `PROACTIVE_FOLLOWUPS` | 1 | Công tắc chung hỏi thăm chủ động |
| `PROACTIVE_ACTIVE_HOURS` / `PROACTIVE_FOLLOWUP_HOUR` | `08:00-21:30` / 18 | Khung giờ được gửi; giờ bắt đầu hỏi trong ngày diễn ra sự việc |
| `PROACTIVE_DAILY_CAP` / `PROACTIVE_IDLE_MIN` / `PROACTIVE_TICK_SEC` | 2 / 20 / 1800 | Tối đa tin/người/ngày; bỏ qua nếu vừa chat trong N phút; chu kỳ vòng hỏi thăm |

Nhắc việc, chi tiêu, bản tin sáng, danh mục:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `EXPENSE_CHAT_CAPTURE` | true | Tin ngắn "cafe 45k" ghi chi tiêu bằng code; false = chỉ khi nói rõ hoặc `/chitieu` |
| `MORNING_BRIEF_ENABLED` | true | Chạy vòng bản tin sáng cá nhân (chỉ gửi người đã bật `/bantin`) |
| `MORNING_BRIEF_DEFAULT_HOUR` | 7 | Giờ gửi khi bật mà không nói giờ (4-12, giờ VN) |
| `MORNING_BRIEF_MAX_MEMBERS` | 20 | Tối đa người dùng Zalo nhận bản tin, kể cả controller (0 = tắt trên Zalo) |
| `MORNING_BRIEF_ABSORB_NEWS` | true | Zalo controller/Zoom bật bản tin thì gộp bản tin RSS 8h vào |
| `MORNING_BRIEF_AI_INTRO` / `MORNING_BRIEF_EXPENSES` | false / true | 1 câu chào AI cho chủ bot; dòng tổng chi tháng |
| `DIGEST_ON_NON_TRADING_DAYS` | false | Gửi digest danh mục 8h cả cuối tuần/ngày lễ (hành vi cũ) |
| `STOCK_FEE_RATE` / `STOCK_SELL_TAX_RATE` | `0.0015` / `0.001` | Phí CTCK mỗi chiều; thuế TNCN trên giá trị bán |
| `PORTFOLIO_EVENTS_ENABLED` / `PORTFOLIO_EVENTS_HOUR_VN` | 1 / 8 | Job danh mục hằng ngày (lưu quy đổi sự kiện quyền, báo lịch quyền, dọn log), khung chạy bù 3 giờ |
| `PORTFOLIO_EVENTS_MAX_HOLDINGS` / `PORTFOLIO_EVENTS_MAX_SYMBOLS` | 60 / 40 | Trần vị thế xử lý và số mã tra lịch quyền mỗi ngày |
| `PORTFOLIO_EVENT_NOTICES` | 1 | Báo trước lịch cổ tức/quyền mua/ĐHCĐ của mã đang giữ |
| `PORTFOLIO_PUSH_ZALO_DAILY_CAP` | 5 | Tin danh mục chủ động tối đa mỗi thành viên Zalo/ngày (0 = không gửi Zalo) |
| `PRICE_ALERTS_ENABLED` / `PRICE_ALERTS_HOLDINGS_INTRADAY` | 1 / 1 | Quét ngưỡng giá trong phiên; báo luôn stop/target danh mục chủ bot trong phiên |
| `PRICE_ALERT_POLL_SEC` | 300 | Chu kỳ quét trong phiên (120-1800 giây) |
| `PRICE_ALERT_MAX_SYMBOLS` / `PRICE_ALERT_MAX_PER_USER` / `WATCHLIST_MAX_SYMBOLS` | 40 / 10 / 20 | Mã mỗi vòng quét; ngưỡng mỗi người; mã theo dõi mỗi người |
| `NOTIFY_HUB_ENABLED` | 1 | Gom thông báo chủ động + giờ yên lặng (`services/notify_hub.py`); 0 = mọi nguồn gửi thẳng như cũ |
| `NOTIFY_BATCH_MINUTES` | 60 | Tin nhẹ chờ tối đa chừng này phút rồi gộp gửi (5-1440) |
| `NOTIFY_QUIET_HOURS` | `22:30-06:30` | Giờ yên lặng mặc định (giờ VN; `off` = không có), từng người đổi bằng `/yenlang` |
| `NOTIFY_QUEUE_MAX_PER_USER` / `RETENTION_NOTIFY_QUEUE_DAYS` | 30 / 7 | Trần tin chờ mỗi người nhận; số ngày giữ dòng đã gửi (chống xếp trùng) |
| `BACKGROUND_JOBS_ENABLED` | 1 | Việc nền `/viec` + bộ chạy tác vụ định kỳ; 0 = tắt cả hai |
| `BACKGROUND_JOB_TIMEOUT_SEC` | 600 | Hạn chót mỗi việc nền/lần chạy tác vụ (60-1800 giây) |
| `BACKGROUND_JOBS_CONCURRENCY` / `BACKGROUND_JOBS_MAX_PER_USER` | 1 / 3 | Việc chạy cùng lúc toàn bot (không vượt `MAX_CONCURRENT_TURNS`); việc chờ/chạy tối đa mỗi người |
| `BACKGROUND_JOBS_MEMBERS` | 1 | Thành viên Zalo được giao `/viec` (tính lượt nặng); 0 = chỉ chủ bot, admin |
| `ROUTINES_ENABLED` / `ROUTINES_MEMBERS` | 1 / 0 | Tác vụ định kỳ `/lichtacvu`; thành viên Zalo mặc định không dùng |
| `ROUTINE_MAX_RUNS_PER_DAY` | 20 | Trần số lần chạy tác vụ định kỳ mỗi ngày toàn bot |
| `RETENTION_BG_JOBS_DAYS` | 30 | Số ngày giữ việc nền đã xong (yêu cầu + kết quả) |

Thoại, ảnh, tài liệu:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `VOICE_STT_ENABLED` | 1 | Chép lời tin thoại Telegram/Zalo; 0 = trả lời "chưa bật" |
| `GROQ_STT_MODEL` / `VOICE_STT_LANGUAGE` | `whisper-large-v3-turbo` / `vi` | Model Whisper của Groq (cooldown riêng `groq_stt`); ngôn ngữ gửi Whisper (trống hoặc `auto` = tự nhận) |
| `VOICE_MAX_BYTES` / `VOICE_MAX_SECONDS` | 5242880 / 180 | Trần tin thoại (64 KB-8 MB; 10-600 giây), gateway dùng chung trần byte |
| `VOICE_REPLY_ENABLED` | 1 | Cho phép `/noi` (mỗi người vẫn phải tự bật); 0 = tắt cả bot |
| `VOICE_REPLY_MAX_CHARS` | 600 | Số ký tự tối đa được đọc (100-2000) |
| `VOICE_TTS_MODEL` / `VOICE_TTS_VOICE` | `gemini-3.8-flash-tts` / `Sulafat` | Model Gemini TTS và giọng dựng sẵn mặc định (Live dùng chung giọng; mỗi người đổi bằng `/noi giong <tên>`) |
| `VOICE_STYLE_PROMPT` | (trống = mặc định) | Chỉ dẫn phong cách đọc bằng lời cho TTS/Live; `off` = không gửi |
| `VOICE_ENGINE` | `auto` | Bộ đọc `/noi`: `auto` = Gemini Live rồi Gemini TTS khi Live lỗi/hết quota/đọc lệch; `live` = chỉ Live; `tts` = chỉ TTS |
| `LIVE_VOICE_MIN_MATCH` | 80 | % giống tối thiểu giữa bản chép lời của Live và văn bản cần đọc (50-100); thấp hơn thì bỏ âm thanh Live |
| `VOICE_REPLY_URL_TTL_MIN` / `VOICE_REPLY_STORE_MB` | 10 / 4 | Zalo/Zoom: link tin thoại ký HMAC sống N phút; kho RAM tối đa N MB (và 12 file) |
| `NL_COMMANDS_ENABLED` / `NL_COMMANDS_LLM` | 1 / 1 | Hỏi tự nhiên thay cho lệnh: 0 = tắt hẳn / 0 = chỉ mẫu câu bằng code, không thêm lệnh vào router tool |
| `NUTRITION_NL` / `NUTRITION_AI_ESTIMATE` | 0 / 0 | `/an`: 1 = ghi bữa ăn từ câu tự nhiên rất rõ; 1 = món lạ ước lượng kcal bằng 1 lượt AI |
| `VISION_QA_ENABLED` / `PHOTO_NO_CAPTION_MODE` | 1 / `prompt` | Ảnh kèm câu hỏi được trả lời về ảnh; ảnh không caption: `prompt` hoặc `describe` |
| `DOCUMENT_QA_ENABLED` / `DOCUMENT_MAX_BYTES` | 1 / 5242880 | Đọc PDF/TXT/MD/DOCX; trần file (64 KB-8 MB) |
| `DOCUMENT_MAX_CHARS` | 40000 | Trần ký tự chữ tách từ TXT/DOCX mỗi lượt hỏi (2000-200000) |
| `OPENROUTER_PDF_ENGINE` | `cloudflare-ai` | Engine miễn phí cho PDF dự phòng qua OpenRouter; giá trị khác hoặc trống = tắt đường này |

Telegram và gateway Zalo:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `TELEGRAM_STATUS_DELAY_SEC` | 7 | Sau N giây gửi 1 dòng trạng thái cho lượt dài (0 = tắt) |
| `TELEGRAM_BUSY_REACTIONS` / `TELEGRAM_ANSWER_BUTTONS` | true / true | 👀/👌/😢 trên tin phải chờ; nút 👍/👎/🔄/✂️ dưới câu trả lời |
| `TELEGRAM_STREAM_DRAFTS` / `TELEGRAM_RICH_MESSAGES` | false / false | Bản nháp chữ đang sinh (sendMessageDraft); tin rich gốc (sendRichMessage) - thử nghiệm |
| `ZALO_DIRECT_DEBOUNCE_MS` / `ZALO_DIRECT_DEBOUNCE_MAX_MS` / `ZALO_DIRECT_DEBOUNCE_MAX_MESSAGES` | 2000 / 6000 / 4 | Gộp tin chữ liên tiếp: chờ sau tin cuối (0 = không chờ), trần chờ, trần số tin |
| `ZALO_SEND_GAP_MIN_MS` / `ZALO_SEND_GAP_MAX_MS` | 600 / 1500 | Nghỉ ngẫu nhiên giữa các đoạn và tin outbox (0 và 0 = tắt) |
| `ZALO_OUTBOX_MAX_PER_HOUR` | 60 | Tin outbox mỗi giờ của tài khoản bot; vượt thì hoãn, không bỏ (0 = không giới hạn) |
| `ZALO_PRESENCE_EVENTS` | 1 | "Đang soạn tin", "đã xem", 👌 khi tin phải chờ (0 = tắt hết) |

Giám sát, gom lỗi, sao lưu, dọn dữ liệu:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `ALERT_ZOOM_MIRROR` | `critical` | Zoom làm kênh phụ cho cảnh báo: `critical` (Telegram lỗi + luôn nhận 🚨), `fallback`, `all`, `off` |
| `RELEASE_NOTIFY` | 1 | Báo chủ bot qua Telegram + Zoom khi đã lên bản mới sau deploy (theo `CHANGELOG.md`); 0 = tắt |
| `WATCHDOG_ENABLED` / `WATCHDOG_MEMORY_PCT` | 1 / 85 | Watchdog luật cố định; ngưỡng % RAM working set (không tính page cache), 2 lần đo liên tiếp |
| `MEMORY_TRIM_INTERVAL_SEC` / `MEMORY_TRIM_AFTER_WORK` / `MEMORY_TRIM_MIN_GAP_SEC` | 600 / 1 / 30 | Trả heap rảnh của Python về hệ điều hành (gc + `malloc_trim`, `core/memtrim.py`) mỗi N giây và sau lượt trả lời/việc nặng (tối đa 1 lần mỗi MIN_GAP giây); 0 = tắt |
| `ZALO_NODE_HEAP_MB` / `ZALO_NODE_OPTIMIZE_SIZE` / `ZALO_NODE_EXTRA_FLAGS` | 96 / 1 / (trống) | Heap V8 của gateway Zalo, `--optimize-for-size` (0 = tắt), cờ node thêm |
| `WATCHDOG_INBOX_STALE_MIN` | 10 | Tin Telegram/Zoom kẹt trong inbox quá N phút thì báo |
| `WATCHDOG_RESTART_GAP_MIN` / `WATCHDOG_RESTART_BURST` | 20 / 3 | Báo khi khởi động lại sau gián đoạn > N phút; khi restart bất thường từ N lần/24 giờ |
| `ERROR_DIGEST_ENABLED` / `ERROR_ALERT_IMMEDIATE` | 1 / 1 | Gom lỗi theo loại + vị trí code (`/loi`); báo ngay loại lỗi mới trong ngày |
| `ERROR_ALERT_MAX_PER_HOUR` / `ERROR_DIGEST_HOUR_VN` | 4 / 7 | Trần tin báo lỗi mới mỗi giờ; giờ gửi tổng hợp lỗi hôm qua (-1 = tắt) |
| `BACKUP_STALE_DAYS` | 3 | Báo khi bản sao lưu mới nhất cũ hơn N ngày (0 = tắt) |
| `RETENTION_SESSION_NOTES_DAYS` / `RETENTION_USER_EVENTS_DAYS` | 365 / 90 | Dọn ghi chú phiên và sự kiện hỏi thăm cũ (0 = tắt) |
| `RETENTION_ANSWER_FEEDBACK_DAYS` / `RETENTION_TURN_METRICS_DAYS` | 180 / 14 | Dọn đánh giá 👍/👎 và số đo từng lượt (0 = tắt) |

Không đặt trên Render:

| Biến | Nơi đặt | Ý nghĩa |
|---|---|---|
| `BACKUP_DATABASE_URL`, `BACKUP_PASSPHRASE` | GitHub Actions secret | URL Session Pooler của role chỉ-đọc `lananh_backup`; passphrase gpg (32+ ký tự ngẫu nhiên) - xem `docs/backup-restore.md` |
| `BACKUP_PG_MAJOR` / `BACKUP_RETENTION_DAYS` | GitHub Actions variable | Bản Postgres client/server cho sao lưu (mặc định 17, ≥ bản Supabase); số ngày giữ artifact (30) |
| `EVAL_GROQ_API_KEY`, `EVAL_OPENROUTER_API_KEY`, `EVAL_GOOGLE_AI_STUDIO_API_KEY` | GitHub Actions secret | Key riêng cho workflow "Evals (golden set)" |
| `EVAL_JUDGE_PROVIDER` / `EVAL_JUDGE_MODEL` | GitHub variable hoặc env local | Giám khảo LLM ghim (`groq` / `openai/gpt-oss-120b`) |
| `RUN_LIVE_EVALS`, `EVAL_MAX_CALLS`, `EVAL_JUDGE` | Env local khi chạy evals | Bật tier B (=1), trần lượt gọi (120), bật giám khảo trong pytest (=1) |

## Chạy local

```bash
git clone https://github.com/<your-username>/lananh.git
cd lananh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt                  # chỉ PyPI
pip check
cp .env.example .env
python main.py
```

Không còn dùng thư viện vnstock: bản 4.x bị nhà phát triển khoá từ 10/11/2026 (kho
vnstocks.com trả 410). Dữ liệu cổ phiếu lấy thẳng REST VCI (`stock/vci_direct.py`) và
DNSE (`stock/providers.py`).

Staging: không tạo service Render thứ hai (750 giờ free tính chung workspace, và
không chạy được gateway Zalo thứ hai). Muốn thử thì tạo 1 bot Telegram test
(BotFather), chạy `python main.py` (long polling) trên máy với `DATABASE_URL` trỏ
Postgres local hoặc DB test riêng - không dùng chung DB production.

Docker:

```bash
docker build -t lananh .
docker run --env-file .env -p 10000:10000 lananh
```

## Deploy Render

1. Tạo Supabase và lấy **Session Pooler** connection string.
2. Tạo Telegram bot và lấy Telegram user ID của chủ sở hữu.
3. Tạo Render Blueprint từ repository.
4. Điền secret trong `render.yaml`.
5. Deploy lần đầu với `ZALO_ENABLED=false`.
6. Kiểm tra `/` trả `200`, `/healthz` trả `200` (có kiểm DB) và Telegram hoạt động.
7. Chỉ bật Zalo sau khi webhook ổn định.
8. Render → Settings → Auto-Deploy phải là "After CI checks pass" (`render.yaml`
   đặt `autoDeployTrigger: checksPass`; Blueprint không tự đồng bộ thì đặt tay).
   Deploy tay trên dashboard vẫn dùng được; commit có `[skip render]` vẫn được bỏ qua.
9. Migration trong `migrations/` tự chạy khi instance mới khởi động; runner so
   checksum với bảng đã áp và báo chủ bot khi lệch. `python -m core.migrations`
   kiểm `migrations.lock` khớp file (CI cũng kiểm).
10. Nên thêm monitor UptimeRobot thứ hai vào `/healthz` và bật Render →
    Settings → Notifications (mục "Giữ Render Free không bị ngủ").

Danh sách việc khi deploy đợt nâng cấp 26/10/2026 (migration 017-025, biến cần đặt,
GitHub secrets cho sao lưu và evals): [docs/nang-cap-2026-10-26.md](nang-cap-2026-10-26.md).

Render đặt cứng:

```env
STOCK_BACKTEST_ALLOW_ON_RENDER=false
```

Nếu code vô tình gọi backtest trên Render, tác vụ dừng trước khi tải dữ liệu hoặc dùng CPU đáng kể. Hãy tạo `stock/data/backtest_stats.json` bằng job CI/offline riêng rồi deploy file kết quả.

### Lưu ý khi deploy lên bản có migration 017 (trí nhớ v2)

Migration chạy ở instance MỚI (`_post_init` → `init_db`) trong lúc Render vẫn để
instance CŨ phục vụ cho tới khi instance mới healthy. `017_memory_meta.sql` bỏ
`UNIQUE(telegram_user_id, key)` của `user_facts` (thay bằng unique index chỉ trên
dòng hiện hành, để giữ lịch sử giá trị cũ), nên trong khoảng chạy chồng đó lệnh
ghi trí nhớ của bản cũ (`ON CONFLICT (telegram_user_id, key)`) lỗi "no unique or
exclusion constraint matching the ON CONFLICT specification": trí nhớ (fact, tóm
tắt) trích từ các lượt chat do bản cũ trả lời trong vài chục giây tới vài phút đó
bị mất, chỉ còn 1 dòng WARNING trong log. Chỉ xảy ra 1 lần ở lần deploy áp 017 và
không ảnh hưởng câu trả lời; muốn tránh hẳn thì deploy lúc không ai chat. Không
giữ được unique index đầy đủ cho bản cũ vì nó chặn chính việc giữ lịch sử fact.

Quy ước từ nay: migration bỏ ràng buộc/cột mà code bản đang chạy còn dùng phải
tách 2 bước - bản phát hành trước thêm cấu trúc mới và ngừng phụ thuộc cái cũ
(expand), bản sau mới bỏ (contract) - vì instance cũ luôn chạy chồng lúc deploy.

## Giữ Render Free không bị ngủ (UptimeRobot)

Render Free tier tự **sleep sau ~15 phút không có request nào tới** — lần
request tiếp theo (vd Telegram gửi webhook) phải đợi service "cold start"
lại, có thể mất vài chục giây tới hơn 1 phút, dẫn tới bot phản hồi trễ hoặc
Telegram coi webhook timeout. Endpoint `/` (health check, không cần auth,
xem `web.py::health`) luôn trả `200` (UptimeRobot nhận `{"status": "ok"}`, trình duyệt
nhận trang chủ), dùng để ping giữ service thức. Khách vào web phải chờ 30-60 giây
nghĩa là monitor này chưa chạy hoặc đã bị tạm dừng - kiểm tra lại trên UptimeRobot:

1. Tạo tài khoản miễn phí tại [UptimeRobot](https://dashboard.uptimerobot.com/login?rt=true).
2. Vào **Add New Monitor**:
   - **Monitor Type**: `HTTP(s)`
   - **Friendly Name**: `lananh` (hoặc tên bất kỳ)
   - **URL**: `https://<tên-service-render>.onrender.com/` (dùng đúng
     `WEBHOOK_BASE_URL` đã cấu hình, có dấu `/` ở cuối)
   - **Monitoring Interval**: `5 minutes` (gói free hỗ trợ tối thiểu 5 phút -
     đủ ngắn để luôn ping trước khi Render kịp sleep sau 15 phút)
3. Lưu monitor. UptimeRobot sẽ tự động gửi GET tới `/` mỗi 5 phút, giữ
   service luôn ở trạng thái "awake".
4. (Tuỳ chọn) Bật thông báo qua email/Telegram/Slack trong UptimeRobot để
   được báo ngay nếu service down thật (không phải do sleep).

Lưu ý: cách này chỉ giữ service khỏi sleep, **không** thay thế việc theo dõi
lỗi thật — vẫn nên kiểm tra log Render định kỳ. Nếu trước đây có dùng
GitHub Actions keep-alive workflow riêng, có thể tắt hẳn vì UptimeRobot làm
đúng việc này mà không cần thêm secret/CI trong repository. Bản tin sáng, hỏi
thăm chủ động, cảnh báo giá trong phiên và các job hằng ngày đều cần service thức.

Giám sát bên ngoài (nên làm, vì watchdog chạy bên trong bot nên không tự báo được
khi chính bot chết):

- Thêm monitor UptimeRobot thứ hai trỏ vào `https://<domain>/healthz` (có kiểm tra
  DB, trả 503 khi DB lỗi hoặc treo), có liên hệ cảnh báo qua email hoặc Telegram.
- Monitor giữ bot thức vẫn để ở `/`; health check của Render vẫn là `/` (không để
  Render restart vì Supabase chập chờn).
- Bật Render → Settings → Notifications để nhận email khi "service unhealthy" hoặc
  deploy thất bại.
