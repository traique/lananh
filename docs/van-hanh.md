# Vận hành: giám sát, truy vết, bảo mật

Cảnh báo, sao lưu, truy vết lượt, quota miễn phí và các lưu ý bảo mật khi vận hành.

Tài liệu chính: [README.md](../README.md).

## Giám sát, cảnh báo, sao lưu

- **Sao lưu DB.** Supabase free không có bản backup để tải về. Workflow GitHub
  Actions "Backup DB" (`.github/workflows/backup.yml`) chạy lúc 04:47 sáng mỗi ngày
  (giờ VN): `pg_dump` schema public qua Session Pooler bằng role chỉ-đọc
  `lananh_backup`, mã hoá AES256 bằng gpg ngay trong pipe rồi lưu artifact (giữ 30
  ngày). Repo public nên chỉ file đã mã hoá được lưu, log không in dữ liệu. Ảnh
  Facebook, webhook inbox, cache chống trùng, thống kê và `turn_metrics` chỉ giữ cấu
  trúc. Ngày 2 hằng tháng có diễn tập khôi phục vào Postgres tạm
  (`scripts/backup_drill.py`, kiểm cả checksum migration). Thiếu secret
  `BACKUP_*` thì job chỉ ghi notice và vẫn xanh. Migration 023 thêm bảng nhịp tim
  `backup_runs`; bot báo khi bản sao lưu cũ hơn `BACKUP_STALE_DAYS` ngày (GitHub tự
  tắt lịch chạy của repo public sau 60 ngày không có commit). Thiết lập, khôi
  phục, xử lý sự cố: [docs/backup-restore.md](backup-restore.md).
  `SETTINGS_ENC_KEY` không nằm trong bản sao lưu - cất khoá này riêng.
- **Xoay khoá `SETTINGS_ENC_KEY`.** Đặt khoá mới vào `SETTINGS_ENC_KEY`, khoá cũ vào
  `SETTINGS_ENC_KEY_OLD` rồi deploy. Lúc khởi động bot tự mã hoá lại các giá trị
  còn dùng khoá cũ (compare-and-swap, `SETTINGS_AUTO_ROTATE=1`), hoặc bấm "Xoay khoá
  ngay" ở `/admin` → Hệ thống. Khi bot báo còn 0 thì xoá `SETTINGS_ENC_KEY_OLD`.
- **Watchdog** (`services/watchdog.py`, luật cố định, không dùng AI, chạy trong vòng
  `MONITOR_INTERVAL_SEC`). Bot báo khi: RAM working set vượt 85% hai lần đo liên
  tiếp, hoặc có OOM kill; DB đạt 80% hoặc 90% `DB_SIZE_LIMIT_MB` (quota Supabase
  tính theo dung lượng trung bình mỗi ngày, cộng cả tổ chức, vượt thì lỗi 402); tin
  Telegram/Zoom kẹt trong inbox quá 10 phút; digest danh mục (ngày giao dịch), bản
  tin RSS 8h, cảnh báo danh mục, job sự kiện quyền hoặc một slot Page chứng khoán
  quá hạn mà chưa chạy (bản tin sáng cá nhân `/bantin` chưa được kiểm); bot khởi động lại vì crash, OOM, gián đoạn hơn 20 phút, hoặc restart
  bất thường dồn dập (deploy và restart do sửa env/Restart service không tính);
  vòng gửi nhắc việc chết vì lỗi bất ngờ (watchdog chạy lại ngay và báo, nhắc đến hạn
  trong lúc dừng được gửi bù kèm nhãn nhắc trễ). Phần nhịp tim/khởi động lại chỉ chạy
  khi `RENDER=true`.
- **Nhắc việc đúng giờ.** Vòng nhắc (`scheduler.py`) hỏi DB còn bao lâu tới nhắc kế tiếp
  rồi ngủ đúng chừng đó, thức ngay khi đặt/hoãn nhắc hoặc thêm ngày nhớ;
  `REMINDER_CHECK_INTERVAL_SEC` (mặc định 120) chỉ là giờ ngủ tối đa.
- **Log.** Ghi stderr qua hàng đợi có trần (1000 dòng; luồng riêng, event loop không chờ
  pipe log); secret bị che trên chuỗi đã format, kể cả traceback, trước khi vào hàng đợi;
  không ghi nội dung lời nói trong tin thoại. Heap khởi động được `gc.freeze()` để mỗi lần
  dọn RAM sau lượt không quét lại; image Docker biên dịch sẵn `.pyc`. Thư viện pandas,
  numpy, matplotlib... đã gỡ khỏi image (chỉ còn trong `requirements-dev.txt` cho script
  offline và test).
- **Phiên bản và báo bản mới.** Số phiên bản và lịch sử cập nhật nằm ở `CHANGELOG.md`
  (mục đầu tiên là bản đang chạy, `core/version.py` đọc). `/phienban [số bản|all]` (chủ
  bot, admin Zalo) xem lịch sử; `/status` có dòng phiên bản kèm mã commit. Khoảng 5 phút
  sau khi bản mới chạy trên Render (vòng giám sát đầu tiên), bot nhắn chủ bot qua
  Telegram và Zoom (nếu đã pair) "🆕 Em đã lên bản mới X.Y.Z" kèm các dòng "Có gì mới";
  restart, ngủ dậy hay sửa env (cùng commit) không báo lại; deploy commit mới mà chưa
  thêm mục CHANGELOG thì báo "deploy lại bản X.Y.Z". Trong giờ yên lặng tin được giữ tới
  sáng. Tắt bằng `RELEASE_NOTIFY=0`.
- **Kênh cảnh báo.** Cảnh báo cùng loại có cooldown (`core.alerts`, trần 128 khoá).
  Telegram là kênh chính; Zoom là kênh phụ khi Telegram lỗi và luôn nhận các cảnh
  báo 🚨 (`ALERT_ZOOM_MIRROR`). Cảnh báo 🚨 và cảnh báo cần xử lý ngay (`urgent=True`:
  hồi phục provider, gateway Zalo mất/có lại kết nối, tin bị bỏ DEAD, Zoom lạ, lượt đăng
  Page chưa rõ, kết quả `/ragxuly`) gửi ngay; cảnh báo nhẹ được gom (mục "Gom thông
  báo và giờ yên lặng" trong docs/tro-ly.md) - tin gom đi Telegram, không nhân bản sang Zoom.
- **Gom lỗi** (`services/error_digest.py`): lỗi ERROR (và WARNING có traceback)
  được gom theo logger, loại lỗi và vị trí code trong repo; không lưu nội dung lỗi.
  Có loại lỗi mới thì bot báo ngay (tối đa 4 tin/giờ); 7h sáng gửi tổng hợp lỗi
  hôm qua. Lệnh `/loi` và thẻ trên `/admin` hiện lỗi hôm nay.
- **Deploy chỉ khi CI xanh.** `render.yaml` đặt `autoDeployTrigger: checksPass`. Quét
  CVE tách sang `.github/workflows/audit.yml` (PR + lịch tuần) nên advisory mới không
  chặn deploy hotfix; CI trên `main` không huỷ run đang chạy (để commit nào cũng có
  check). CI chạy thử image Docker (import, `pip check`, node, zca-js).
- **Migration.** Schema chỉ đổi bằng file `migrations/NNN_*.sql` mới. Sau khi thêm
  file, chạy `python -m core.migrations --write-lock` rồi commit `migrations.lock`;
  `test/test_migration_lock.py` đỏ nếu sửa migration đã áp. Merge bị xung đột ở
  `migrations.lock` thì chạy lại lệnh đó.
- **Phương án rời Render** nếu bot hay bị gián đoạn (đều dùng lại Dockerfile và
  supervisord): Oracle Cloud Always Free (A1) hoặc GCE e2-micro chạy liên tục nhưng
  phải tự lo TLS và cập nhật OS (Oracle có thể thu hồi VM rảnh); Fly.io
  shared-cpu-1x 512 MB khoảng 3,69 USD/tháng ở iad, không có gói free cho tổ chức mới.

## Truy vết lượt, quota miễn phí

- Mỗi câu trả lời có 1 mã lượt: `telegram:<update_id>`, `zalo:<account>:<message_id>`,
  `zoom:<account>:<event_id>`; job nền có mã `job:<tên>:<ngày>` (`core/trace.py`).
  Mã lượt nằm ở đầu mọi dòng log dạng `[telegram:123] ...` và trong
  `provider_calls.turn_id` (migration 025): tìm mã lượt trong log Render là ra cả lượt.
- `provider_calls` ghi thêm token vào/ra do provider tự báo (Groq, OpenRouter,
  Gemini) và số ký tự prompt (`ai/call_log.py` là nơi ghi duy nhất). Gemini dự phòng
  của lượt phụ, từng bước `/agent`, Agnes (`/anh`) và Tavily đều được ghi, kể cả lượt
  lỗi hoặc bị 429.
- Bảng `turn_metrics` ghi chờ khoá, chờ slot, gom ngữ cảnh, LLM, gửi tin (Telegram),
  provider cuối, có đổi provider không, kết quả (ok/error/timeout/cancelled); ghi
  nền có trần, giữ 14 ngày (`RETENTION_TURN_METRICS_DAYS`), bản sao lưu không chép
  dữ liệu bảng này. Gateway Zalo chưa ghi cùng mã lượt và chưa đo thời gian gửi.
- `/status` (Telegram, Zalo/Zoom) có khối "📈 24 giờ qua": số lượt, p50/p95 độ trễ,
  tỉ lệ lỗi theo từng provider, và dòng "🎙️ Gemini Live": bật/tắt, cooldown riêng,
  số lượt Live trong ngày quota (từ 0h giờ Thái Bình Dương, đọc `provider_calls`;
  không ping Live để khỏi tốn lượt). `/admin` → Quota miễn phí có dòng "Gemini Live".
- Quota miễn phí (`services/quota_burn.py`), mỗi provider đếm theo đúng cửa sổ của
  nó: Gemini theo project Google Cloud, reset 0h giờ Thái Bình Dương (khoảng 14-15h
  giờ VN; 2 key cùng project thì `GEMINI_KEYS_SAME_PROJECT=1`); OpenRouter theo ngày
  (`FREE_TIER_RPD_OPENROUTER`, mặc định 50; đặt 1000 nếu đã nạp từ 10 USD); Tavily
  theo tháng (1000 credit, tìm "advanced" tính 2); Groq lấy số còn lại từ header
  response. Hạn mức để 0 thì chỉ hiển thị. Chạm `QUOTA_BURN_ALERT_PCT` (80%) thì bot
  nhắn chủ bot 1 lần cho mỗi cửa sổ. Nên đặt `FREE_TIER_RPD_GEMINI` theo hạn mức
  của model đang dùng (xem trên AI Studio). Hạn mức free tier của Gemini Live chưa
  được Google công bố: quan sát số lượt trên `/status` và aistudio.google.com/rate-limit
  rồi mới đặt `FREE_TIER_RPD_GEMINI_LIVE`.
- Nếu Groq từ chối tham số xin token thì đặt `GROQ_STREAM_USAGE=0`.

## Bảo mật vận hành

- Không commit `.env`, token 9Router, Zalo session, Zoom secret, QR hoặc media.
- Không log credential hoặc signed Zalo CDN URL đầy đủ.
- Không public port `9901`. `/internal/zalo/*` chỉ nhận request loopback.
- Webhook Zoom có timestamp lệch quá 5 phút bị từ chối (chống replay).
- Dùng secret riêng cho webhook, diagnostics và Zalo bridge.
- Giữ `SETTINGS_ENC_KEY` ổn định và backup an toàn. Xoay khoá: đặt khoá mới vào
  `SETTINGS_ENC_KEY`, khoá cũ vào `SETTINGS_ENC_KEY_OLD` (đọc được cả hai, ghi
  bằng khoá mới), sau khi mọi giá trị đã ghi lại thì bỏ biến cũ. Bật
  `SETTINGS_REJECT_PLAINTEXT=1` khi DB không còn giá trị chưa mã hoá.
- Log che mọi secret cấu hình (token Telegram, mọi API key, mật khẩu trong
  `DATABASE_URL`, token Facebook, `ADMIN_PASS`...), không chỉ 6 giá trị như trước.
- `DATABASE_SSL_MODE=verify-full` + `DATABASE_SSL_CA` để xác thực chứng chỉ
  Supabase (mặc định `require` chỉ mã hoá, không xác thực).
- Web và `/admin` gửi header bảo mật (nosniff, Referrer-Policy, X-Frame-Options
  DENY, CSP frame-ancestors 'none', HSTS không preload; `SECURITY_HSTS=0` để tắt
  HSTS); middleware không ghi đè header do route tự đặt.
- Telegram tắt link preview cho mọi tin bot gửi; link lạ trong câu trả lời bị vô
  hiệu (`REPLY_URL_FILTER`); dữ liệu ngoài luôn được rào bằng `core/untrusted.py`.
- `/healthz` kiểm tra DB thật (`SELECT 1`), `/` vẫn nhẹ cho Render health check.
  Container vẫn chạy root: Render không hỗ trợ supervisord đổi user (xem ghi chú
  trong `supervisord.conf`).
- Chỉ chạy một Zalo listener cho tài khoản bot.
- Rotate session ngay nếu nghi ngờ bị lộ.
