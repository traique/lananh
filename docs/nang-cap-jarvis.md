# Nâng cấp lấy ý từ các dự án Jarvis

Các tính năng mới lấy ý tưởng từ isair/jarvis, ethanplusai/jarvis,
amanimran786/jarvis-ai, microsoft/JARVIS. Mỗi gói một mục; chi tiết cách dùng nằm
trong `README.md`.

## Gói P1: "Có gì cần anh chú ý?" + gom thông báo, giờ yên lặng

### Đã làm

- **`/chuy`** (ý từ amanimran786/jarvis-ai "what needs my attention",
  `services/attention.py`): MỘT tin tính bằng code, không gọi AI, trên Telegram,
  Zalo, Zoom. Câu tự nhiên rõ ràng ("có gì cần anh chú ý", "hôm nay có gì cần lo")
  cũng được - phải có người được nhắc hoặc mốc "hôm nay/bây giờ", câu dài quá 80
  ký tự không tính, nên "có gì cần lưu ý khi mua nhà" vẫn đi chat AI. Registry mục
  giống `morning_brief` (`register_section(key, order, timeout=, audience=)`, audience
  `all`/`admin`/`owner`) để gói sau thêm "việc nền đang chạy", "tác vụ định kỳ". Mục
  có sẵn: nhắc việc còn lại hôm nay + ngày nhớ 3 ngày tới; danh mục (stop/target và
  giá chạm ngưỡng đã báo hôm nay, đang dưới stop từ 2 phiên, sự kiện quyền rơi vào hôm
  nay - đọc nhật ký `stock_alert_log` và trạng thái job cuối phiên, không gọi nguồn
  giá); hỏi thăm đang chờ (hôm nay/mai); số tin đang chờ gom; bài Facebook chờ duyệt
  (chủ bot, admin Zalo); lỗi ERROR hôm nay, provider cooldown, quota miễn phí ≥
  `QUOTA_BURN_ALERT_PCT`, watchdog đang báo (`watchdog.active_issues`, chỉ chủ bot:
  Telegram/Zoom và Zalo controller). Mỗi mục có timeout 6-8 giây; mục lỗi bị bỏ và
  ghi "chưa đọc được: ...". Thành viên Zalo chỉ thấy dữ liệu theo internal_user_id của
  mình. `/chuy` và `/yenlang` (không tham số) là lệnh chỉ đọc: chạy ngoài khoá lượt,
  gateway Zalo đưa ra làn nhanh (`zalo-gateway/src/commands.ts`).
- **Gom thông báo + giờ yên lặng** (ý từ ethanplusai/jarvis,
  `services/notify_hub.py`, migration 026 bảng `notify_queue`): việc cần người quyết
  báo ngay, việc đã xong/thông tin gom lại báo một lần. `/yenlang on|off|22-7`.

### Nguồn tự nhắn và mức

| Nguồn | Mức | Ngoài giờ yên lặng | Trong giờ yên lặng (mặc định 22:30-06:30) |
|---|---|---|---|
| Nhắc việc, ngày nhớ (`scheduler`) | URGENT | Gửi ngay (không qua hub) | Gửi ngay |
| Stop/target trong phiên (`price_alerts`) | URGENT | Gửi ngay | Giữ lại (thực tế chỉ có trong giờ giao dịch) |
| Giá chạm ngưỡng người dùng đặt | URGENT | Gửi ngay | Giữ lại (như trên) |
| Cảnh báo cuối phiên (`portfolio_alerts`) | URGENT nếu có dòng stop/target, còn lại NORMAL | Gửi ngay / gom | Không xảy ra (15h05-18h05) |
| Bài Facebook chờ duyệt (bản xem trước) | URGENT | Gửi ngay, kèm nút | Giữ lại cho Telegram + Zalo controller, gửi đầu tiên khi hết giờ (không nút, vẫn có `/fb_ok`, `/fb_boqua`); bản Zoom bỏ |
| Cảnh báo 🚨 (`core.alerts`: OOM, crash, toàn bộ provider chết, Zalo cần đăng nhập lại) | URGENT | Gửi ngay | Gửi ngay |
| `core.alerts.send(urgent=True)`: provider hồi phục, gateway Zalo mất/có lại kết nối, tin bị bỏ (DEAD/không thử lại), Zoom lạ nhắn, lượt đăng Page chưa rõ, kết quả `/ragxuly` | URGENT | Gửi ngay | Giữ lại |
| Cảnh báo nhẹ `core.alerts` (watchdog ⚠️/💽/⏰, lỗi mới 🐞, tổng hợp lỗi 🧾, quota ⛽, 9Router lỗi/hồi phục, migration bị sửa, nghi prompt injection, outbox Zalo bỏ cuộc, sao lưu cũ, xoay khoá, nhắc cập nhật lịch giao dịch) | NORMAL | Gom | Chờ |
| Quy đổi sự kiện quyền, lịch quyền báo trước (`portfolio_events` qua `portfolio_push`) | NORMAL | Gom | Chờ |
| Hỏi thăm chủ động (`proactive_service`) | NORMAL | Gom | Không xảy ra (giờ hoạt động 08:00-21:30) |
| Bản tin sáng (`morning_brief`), digest danh mục 8h (`scheduler`) | NORMAL, tin tổng hợp | Xả ngay, kèm tin NORMAL đang chờ | Chờ hết giờ |

Không qua hub (gửi như cũ): câu hỏi cấp quyền người Zalo lạ (kèm nút, cần quyết
ngay), bản tin RSS 8h `morning_news` và tổng kết nhóm Zalo 9h (nội dung định kỳ ngoài
giờ yên lặng, guard riêng; Zalo controller bật `/bantin` thì RSS đã gộp vào bản tin
sáng), báo cáo VACUUM của `db_maintenance` (chỉ khi thật sự dọn, 3h sáng Chủ nhật),
trả lời trực tiếp của mọi lượt chat.

### Cách xả và chống trùng

- NORMAL vào `notify_queue` (bền qua restart/redeploy). Vòng 5 phút xả người nhận có
  tin cũ nhất đã chờ `NOTIFY_BATCH_MINUTES` (60) phút, có tin URGENT bị giữ hoặc tin
  tổng hợp chưa gửi được, hoặc có tin xếp trong giờ yên lặng vừa hết. Người dùng vừa
  nhắn bot (Telegram, Zalo, Zoom) thì xả sau 8 giây, kể cả ban đêm (họ đang thức).
- Mọi tin chờ của 1 người nhận gộp thành MỘT tin ("🔔 Em gom N thông báo từ HH:MM
  cho anh nè:"), URGENT bị giữ đứng đầu; chỉ 1 tin thì gửi nguyên văn. Trần 8.000 ký
  tự/tin gom, dư thì tin gom tiếp theo ngay sau.
- Nguồn chỉ đánh dấu guard của mình (ngày đã gửi, `stock_alert_log`, sự kiện hỏi
  thăm `asked`...) khi hub đã nhận tin vào hàng đợi bền; `dedup_key` (vd
  `brief:zalo:-5:2026-10-26`, `followup:<id>`, `corp:<ref>:<ngày>`) chặn xếp 2 lần khi
  nguồn chạy lại. Dòng đã gửi giữ `RETENTION_NOTIFY_QUEUE_DAYS` (7) ngày.
- Zalo: đánh dấu đã gửi và INSERT `zalo_outbox` trong CÙNG transaction - không bao
  giờ gửi đôi, gateway vẫn gửi qua outbox (ngân sách 60 tin/giờ). Telegram/Zoom: lease
  5 phút; gửi lỗi thì nhả và vòng sau thử lại.
- Tối đa `NOTIFY_QUEUE_MAX_PER_USER` (30) tin chờ/người; vượt thì bỏ tin NORMAL cũ
  nhất, tin gom kế tiếp ghi "có N thông báo cũ hơn đã được lược bớt".
- Hub chỉ nhận tin khi vòng xả đã chạy trong process (`bot_app._post_init`); DB lỗi lúc
  xếp hàng thì nguồn gửi thẳng như cũ (fail open). `NOTIFY_HUB_ENABLED=0` = hành vi cũ
  hoàn toàn.

### Việc khi deploy

1. Migration 026 tự áp lúc khởi động; `migrations.lock` đã có dòng 026.
2. Biến mới (đều có mặc định an toàn, có trong `.env.example` và `render.yaml`):
   `NOTIFY_HUB_ENABLED=1`, `NOTIFY_BATCH_MINUTES=60`, `NOTIFY_QUIET_HOURS=22:30-06:30`,
   `NOTIFY_QUEUE_MAX_PER_USER=30`, `RETENTION_NOTIFY_QUEUE_DAYS=7`.
3. Menu Telegram tự cập nhật (`/chuy`, `/yenlang`). Gateway Zalo cần build lại (đã
   nằm trong image) để `/chuy` đi làn nhanh.
4. Thử: `/chuy` trên Telegram và từ 1 Zalo thành viên (không được thấy lỗi/provider/
   bài FB); `/yenlang` xem giờ. Khi có 1 tin nhẹ đang chờ (`/chuy` hiện dòng "📬 Có N
   thông báo"), nhắn bot 1 câu - tin gom phải tới vài giây sau câu trả lời.

### Quyết định đã chọn (chủ bot có thể đổi)

- URGENT khác nhắc việc và 🚨 trong giờ yên lặng: GIỮ lại tới hết giờ (không gửi im
  tiếng), vì Zalo không có chế độ im tiếng và tin chạm giá/stop thực tế chỉ có trong
  phiên. Bài FB giữ lại mất nút bấm.
- Tin gom của chủ bot đi Telegram, không nhân bản sang Zoom (luật `ALERT_ZOOM_MIRROR`
  chỉ còn áp cho tin gửi ngay). Telegram lỗi thì tin gom nằm chờ và thử lại, không
  chuyển Zoom.
- Kết quả `/ragxuly`, lượt đăng Page chưa rõ, gateway Zalo mất kết nối, tin bị DEAD:
  coi là URGENT (gửi ngay ban ngày) dù không mang 🚨.
- Người dùng nhắn bot trong giờ yên lặng thì vẫn xả tin chờ.

### Cố ý chưa làm

- Gửi im tiếng (`disable_notification`) cho Telegram trong giờ yên lặng: cần thêm
  tham số qua mọi callback gửi; giữ lại đơn giản hơn và giống nhau giữa các kênh.
- Đưa nhắc việc vào hàng đợi: nhắc việc cần nút hoãn, `reminder_id` của outbox Zalo
  và luôn gửi đúng giờ - giữ đường cũ.
- Gom bản tin RSS 8h, tổng kết nhóm Zalo: đã có guard ngày riêng, gửi ngoài giờ yên lặng.

### Rủi ro

- Telegram: sập đúng giữa lúc gửi và lúc đánh dấu thì tin gom có thể lặp 1 lần (như
  các nguồn at-least-once cũ). Tin gom dùng `send_rich` (định dạng markdown nhẹ) cho cả
  cảnh báo hệ thống trước đây gửi chữ thô; Telegram từ chối định dạng thì tự rơi về chữ
  thô.
- Cảnh báo nhẹ (RAM cao, lỗi mới...) có thể tới muộn tới 60 phút nếu chủ bot không
  nhắn bot; `/chuy` luôn xem được ngay.
- Vượt trần 30 tin chờ thì tin nhẹ cũ nhất bị bỏ (có ghi số tin bị lược).
- Bản tin sáng trước 6:30 (`/bantin 5`) giờ tới lúc hết giờ yên lặng; muốn nhận 5h thì
  `/yenlang 22-5` hoặc `/yenlang off`.
- Câu tự nhiên `/chuy` dựa trên mẫu cố định; câu lạ ("có chuyện gì anh phải biết
  không") vẫn đi chat AI.

## Gói P2: việc nền có trạng thái + tác vụ định kỳ bằng lời

Hai tính năng dùng chung MỘT bộ chạy lượt AI nền (`services/background_jobs.py`,
migration 027: bảng `bg_jobs`, `routines`).

### 1. Việc nền `/viec` (ý từ ethanplusai/jarvis runs, isair/jarvis Task Centre)

- `/viec <yêu cầu>` xếp 1 dòng `bg_jobs` (queued) rồi trả lời ngay "⏳ Em nhận việc #12
  rồi, em làm xong báo anh nha". `/viec` liệt kê (đang chạy/chờ trước, 10 dòng),
  `/viec xem <số>` xem kết quả đã lưu, `/viec huy <số>` huỷ. Mọi kênh (Telegram, Zalo,
  Zoom); `/viec` không tham số là lệnh chỉ đọc (làn nhanh gateway Zalo).
- Tiêu chí chạy nền: CHỈ khi người dùng gõ `/viec`. `/agent`, đọc tài liệu lớn vẫn chạy
  trong lượt như cũ (đoán trước "sẽ lâu" dễ sai; người dùng tự chọn rõ ràng hơn).
- Trạng thái: queued → running → succeeded | failed | cancelled | timed_out; lưu yêu
  cầu, kết quả cắt còn tối đa 6.000 ký tự, lỗi, số lần thử, provider, token (cộng từ
  `provider_calls` của lượt qua `core/trace.py`), các mốc thời gian.
- Chạy NGOÀI khoá lượt `assistant_turn` (người dùng chat bình thường trong lúc chờ)
  nhưng vẫn lấy 1 chỗ trong semaphore tổng: `concurrency.background_slot(key)` - việc
  của thành viên Zalo lấy cả chỗ thành viên như 1 lượt chat của họ (không bao giờ
  đụng chỗ dành riêng `OWNER_RESERVED_SLOTS`), việc của chủ bot dùng chỗ của chủ bot.
  Tổng lượt AI (chat + nền) vẫn ≤ `MAX_CONCURRENT_TURNS` → RAM đỉnh không đổi.
- Toàn bot tối đa `BACKGROUND_JOBS_CONCURRENCY` (1, không vượt `MAX_CONCURRENT_TURNS`)
  việc chạy cùng lúc; mỗi người tối đa `BACKGROUND_JOBS_MAX_PER_USER` (3) việc chờ/chạy;
  cả hàng chờ tối đa 50 việc.
- Hạn chót riêng `BACKGROUND_JOB_TIMEOUT_SEC` (600, kẹp 60-1800) đặt bằng
  `turn_deadline` nên mọi lượt gọi AI vẫn qua `bounded()`; thêm `asyncio.wait_for`
  dự phòng (+30 giây). Hết hạn → `timed_out` và báo người dùng.
- Lượt AI = `channel_chat_service.run_chat_turn` của đúng người đó (persona, trí nhớ,
  `web_search.plan_search`, số liệu vàng/tỷ giá/xổ số, tool đọc danh mục/chi tiêu, số
  liệu giá cổ phiếu nhắc tới; thành viên Zalo vẫn chỉ nhận bản tham khảo cổ phiếu) với
  `allow_side_effects=False`. Tham số mới của `run_chat_turn`: `deadline_sec`,
  `record_history=False` (bộ chạy nền tự lưu kết quả với nhãn "[Việc nền #12] ..." loại
  `background` - không trích trí nhớ), `telemetry_kind` ("viec"/"routine").
- Kết quả gửi qua `notify_hub.send` (hàm mới: `offer` trước, hub không nhận thì gửi
  thẳng Telegram/Zoom/zalo_outbox) với nguồn `background_job` mức URGENT: ban ngày gửi
  ngay, trong giờ yên lặng giữ tới hết giờ như các URGENT khác. Tin dài: Telegram tự
  chia tin, outbox Zalo do gateway chia.
- Huỷ: dòng DB chuyển `cancelled` ngay; việc đang chạy trong process bị huỷ TASK CON
  (task worker vẫn sống, nhận việc tiếp); instance khác đang chạy việc đó tự dừng ở
  nhịp gia hạn lease 30 giây kế tiếp. `/huy` không đụng việc nền.
- Khởi động lại (đã chọn: CHẠY LẠI 1 LẦN): việc running có lease 120 giây gia hạn mỗi
  30 giây. Process chết/redeploy → lease hết hạn → vòng dọn (mỗi 60 giây) đưa việc về
  hàng chờ; bị gián đoạn lần thứ 2 thì `failed` (lỗi `interrupted`) và báo "bị gián
  đoạn vì bot khởi động lại". Việc không ghi dữ liệu nên chạy lại an toàn; 2 instance
  chạy chồng lúc deploy không nhận trùng (`FOR UPDATE SKIP LOCKED` + lease).
- Thành viên Zalo: mặc định dùng được (`BACKGROUND_JOBS_MEMBERS=0` để tắt); `/viec
  <yêu cầu>` là lệnh NẶNG trong `usage_quota` (hết lượt nặng thì bị chặn như `/anh`),
  lúc chạy ghi `user_calls` kind "viec" (tính lượt nặng). Admin Zalo, chủ bot không
  giới hạn.

### 2. Tác vụ định kỳ bằng lời (ý từ amanimran786/jarvis-ai routines)

- "mỗi sáng thứ 2 8h tóm tắt tin về VNM cho anh", "mỗi tối thứ 6 9h tổng kết chi tiêu
  tuần cho anh" → 1 dòng `routines` (`services/routines.py`). Thời gian/lặp đọc bằng
  `vn_time_parse.extract_reminder` + `recurrence` (cùng JSON với `reminder_series`),
  không viết bộ đọc mới. Sửa kèm: "mỗi sáng thứ 2 8h" trước bị đọc thành hằng NGÀY,
  giờ là hằng tuần vào thứ 2 (áp cả cho `/nhac`).
- Khác nhắc việc: nhắc việc gửi 1 câu cố định đúng giờ; tác vụ chạy 1 yêu cầu AI.
- Câu tự nhiên chỉ được nhận khi chắc (ưu tiên an toàn): có cụm "tóm tắt / tổng kết /
  tổng hợp / báo cáo / kiểm tra / tra (cứu) / điểm tin / thống kê / cập nhật / liệt kê"
  (kiểm cả dấu: "trả"/"trà" không tính) + "cho/giúp/gửi <anh/chị/em...>" + mốc lặp lại
  có giờ, và KHÔNG có "nhắc/hẹn giờ/báo thức" (câu đó vẫn là nhắc việc). Kiểm TRƯỚC
  router AI nên không thành nhắc việc. Có thứ + giờ nhưng thiếu "mỗi" ("tối thứ 6 9h
  tổng kết chi tiêu tuần cho anh") thì hỏi lại "anh muốn em làm HẰNG TUẦN ... đúng
  không?", nhắn "ok" (10 phút, tin kế tiếp) mới đặt; có "mỗi" mà thiếu giờ thì hỏi
  giờ. Câu khác ("kiểm tra giúp anh 8h tối nay có mưa không") đi chat như cũ.
- `/lichtacvu` xem (lịch, lần tới, kết quả lần trước), `/lichtacvu <câu có mốc lặp>`
  đặt, `/lichtacvu huy|tam dung|tiep <số>`.
- Đến giờ: vòng dọn của bộ chạy nền gọi `routines.run_tick` mỗi 60 giây - nhận lần
  chạy bằng `UPDATE ... WHERE next_run_at = <cũ>` và xếp `bg_jobs` (kind `routine`,
  `dedup_key` theo giờ hẹn) trong cùng transaction → cùng hạn chót, cùng semaphore,
  cùng lượt AI không ghi dữ liệu. Gợi ý cho model: tác vụ định kỳ, đi thẳng vào điều
  đáng chú ý, không có gì thì trả đúng `NO_REPLY` → không gửi gì (vẫn lưu trạng thái
  "không có gì đáng báo").
- Kết quả: nguồn `routine` mức NORMAL, kiểu tin tổng hợp hẹn giờ như bản tin sáng
  (ngoài giờ yên lặng xả ngay kèm tin nhẹ đang chờ, trong giờ yên lặng chờ hết giờ).
- Trần: tối đa 10 tác vụ/người; 2 lần chạy cách nhau ≥ 1 giờ (lặp theo ngày trở lên
  nên luôn thoả, vẫn kiểm); tối đa `ROUTINE_MAX_RUNS_PER_DAY` (20) lần chạy/ngày toàn
  bot - vượt thì bỏ các lần còn lại trong ngày và báo chủ bot 1 lần.
- Lỡ giờ (Render ngủ, redeploy): chạy bù ĐÚNG 1 lần nếu trễ ≤ 12 giờ (tin ghi "chạy bù
  lịch 08:00 ..."), các lần lỡ khác bỏ; trễ hơn 12 giờ thì bỏ lần đó. Lịch kế tiếp tính
  từ bây giờ.
- Thành viên Zalo: mặc định TẮT (`ROUTINES_MEMBERS=0`; câu tự nhiên của họ đi chat như
  thường). Mở thì mỗi lần chạy tính 1 lượt nặng; hết lượt nặng hôm đó thì bỏ lần chạy.

### Hiển thị

- `/chuy`: mục "⏳ Việc nền em đang làm cho anh" (việc chờ/chạy của chính người hỏi)
  và "🔁 Tác vụ định kỳ hôm nay" (đã gửi / không có gì đáng báo / lỗi / sắp chạy).
- `/admin` → Hệ thống → "Việc nền & tác vụ định kỳ" (`/admin/api/jobs`).
- `/xoadulieu`, `/zaloxoa` của thành viên Zalo xoá cả `bg_jobs`, `routines`; `/xoadulieu`
  của chủ bot xoá việc nền đã xong (tác vụ định kỳ giữ như nhắc việc).
  `RETENTION_BG_JOBS_DAYS` (30) dọn việc đã xong.

### Việc chủ bot cần làm

- Migration 027 tự áp lúc khởi động; `migrations.lock` đã có dòng 027.
- Biến mới (mặc định an toàn, đã có trong `.env.example`, `render.yaml`):
  `BACKGROUND_JOBS_ENABLED=1`, `BACKGROUND_JOB_TIMEOUT_SEC=600`,
  `BACKGROUND_JOBS_CONCURRENCY=1`, `BACKGROUND_JOBS_MAX_PER_USER=3`,
  `BACKGROUND_JOBS_MEMBERS=1`, `ROUTINES_ENABLED=1`, `ROUTINES_MEMBERS=0`,
  `ROUTINE_MAX_RUNS_PER_DAY=20`, `RETENTION_BG_JOBS_DAYS=30`.
- Menu Telegram tự cập nhật (`/viec`, `/lichtacvu`). Gateway Zalo cần build lại để
  `/viec`, `/lichtacvu` không tham số đi làn nhanh (`zalo-gateway/src/commands.ts`).
- Thử: `/viec so sánh 3 gói cước 5G` rồi nhắn 1 câu khác ngay (phải trả lời bình
  thường), chờ tin "✅ Em làm xong việc #..."; `/lichtacvu mỗi ngày <giờ sắp tới>
  kiểm tra giá vàng cho anh`, chờ tới giờ.

### Rủi ro

- Với `MAX_CONCURRENT_TURNS=2` (mặc định), chỗ thành viên chỉ có 1: trong lúc việc nền
  của 1 thành viên chạy, thành viên khác phải chờ (gateway xếp hàng) tới khi việc xong
  - thường dưới 1 phút, tối đa `BACKGROUND_JOB_TIMEOUT_SEC`. Việc của chủ bot giữ chỗ
  của chủ bot: chủ bot chat trong lúc đó thì chiếm luôn chỗ còn lại, thành viên chờ
  lượt chat của chủ bot xong. Muốn thành viên không bao giờ bị ảnh hưởng thì
  `BACKGROUND_JOBS_MEMBERS=0`.
- Chạy lại sau khởi động lại tốn thêm 1 lượt quota provider; kết quả có thể tới 2 lần
  nếu process chết đúng giữa lúc gửi và lúc ghi trạng thái (hiếm).
- Câu tự nhiên dựa trên mẫu cố định: câu lạ ("sáng nào cũng xem giúp anh tin VNM") đi
  chat AI - router có thể đặt thành nhắc việc gửi câu cố định; dùng `/lichtacvu` cho
  chắc. Câu "ok" chờ xác nhận nằm trong RAM: khởi động lại thì nhắn lại câu đầy đủ.
- `NO_REPLY` do model tự quyết: có thể im khi đáng báo hoặc ngược lại.

## P3 — Che bí mật, duyệt cách trả lời, mô tả tool gọn

### 1. Che dữ liệu nhạy cảm trước khi gửi provider (ý từ isair/jarvis)

- Vấn đề: `services/memory_guard` chỉ chặn khi LƯU trí nhớ; câu hỏi, lịch sử chat,
  trí nhớ, grounding vẫn gửi nguyên văn cho các provider miễn phí.
- `services/prompt_redaction.py` (`Redactor`) thay phần GIÁ TRỊ bí mật bằng nhãn
  `[đã ẩn: mã OTP]`, `[đã ẩn: mật khẩu]`, `[đã ẩn: số tài khoản]`, `[đã ẩn: số
  thẻ]`... (nhiều giá trị cùng loại thì đánh số `[đã ẩn: mật khẩu 2]`) và bỏ ký tự
  vô hình. Mẫu: `memory_guard.prompt_secret_spans` - cùng từ khoá neo với
  `memory_guard` (OTP, mật khẩu/mk/pass, PIN, STK, CCCD/CMND/hộ chiếu, thẻ kiểm
  Luhn, khoá bí mật, tiền tố API key) nhưng chặt hơn để không ẩn nhầm: giá trị
  phải trông như mật khẩu (không phải "là gì", "15tr", "5000mAh"), "pass" chỉ khi
  liền dấu tách hoặc "pass wifi/mail...", STK/CCCD không nhận số có dấu chấm tiền
  hay đuôi "đồng", thẻ không từ khoá phải viết theo nhóm 4 số.
- Ráp ở chỗ gửi provider: `ai/orchestrator` `chat` (câu hỏi, lịch sử, trí nhớ,
  grounding - trước khi cắt ngân sách), `ask`, `utility_json`, `analyze_image`,
  `analyze_document`; `ai/agent_service` (câu hỏi, lịch sử, trí nhớ, kết quả
  tool). Chữ chép từ tin thoại đi qua `chat` nên cũng được che. Nội dung file ảnh/
  PDF/âm thanh gửi nguyên (không đọc được để che).
- Có ẩn thì model nhận thêm 1 dòng hệ thống: thông tin đã được che, không đoán,
  không nhắc lại, không hỏi người dùng gửi lại.
- DB lưu như cũ. Router tool chat chạy trong `prompt_redaction.scope()` rồi
  `restore_obj` tham số: "ghi chú giúp anh: mật khẩu wifi là ..." vẫn lưu đúng
  ghi chú (provider chỉ thấy nhãn). Harness eval router làm giống hệt.
- Tắt: `PROMPT_REDACTION=0`.

### 2. Bài học cách trả lời phải được duyệt (ý từ amanimran786/jarvis-ai)

- Trước: bộ trích nhớ tự ghi fact `cach_tra_loi` - đoán sai là mọi lượt sau trả
  lời lệch.
- Giờ (`services/style_approval.py`, `STYLE_RULES_NEED_APPROVAL=1` mặc định):
  quy tắc bộ trích xuất đề xuất được so dòng với quy tắc đang áp (thêm/bỏ dòng)
  và lưu ở settings `style_pending:<user_id>` (JSON 1 dòng/người, tối đa 8 dòng
  mỗi loại, hết hạn sau 3 ngày). Đề xuất mới khi còn đề xuất cũ thì gộp; lúc
  duyệt áp diff lên quy tắc HIỆN HÀNH (không đè lời người dùng vừa tự dặn).
- Hỏi 1 lần cho mỗi đề xuất, tối đa 1 lần/ngày/người, bằng tin RIÊNG ngay sau câu
  trả lời chat (không chen giữa câu trả lời): Telegram có nút [✅ Ghi nhớ] [🗑 Bỏ]
  (verb `hoc` trong whitelist `services/action_buttons.py`, chỉ chủ bot bấm);
  Zalo/Zoom nhắn "ghi nhớ"/"bỏ" là tin kế tiếp ngay sau câu hỏi (6 giờ), hoặc
  `/hoc ok|bo` bất cứ lúc nào (mọi kênh; `/hoc` xem đề xuất).
- Bỏ đề xuất: các dòng đó không được đề xuất lại (danh sách có trần 20).
- Lời người dùng tự dặn rõ vẫn áp ngay: `/nho cách trả lời: ...`, câu có mốc lâu
  dài + chuyện trình bày ("từ giờ trả lời ngắn thôi", "lần sau đừng chèn emoji",
  "đừng ... nữa"). Câu chung chung ("trả lời ngắn thôi em") thành đề xuất.
- Mỗi người (Zalo tách riêng, kể cả Zalo của chủ bot) có khoá riêng; `/forget`,
  `/xoadulieu`, `/zaloxoa` xoá luôn đề xuất chờ.
- Sửa kèm: quy tắc cách trả lời giữ mỗi dòng 1 quy tắc (trước bị gộp thành 1 dòng
  do `memory_guard.clean` gộp cả xuống dòng); bản cũ "- A - B" vẫn tách được.

### 3. Mô tả tool ngắn gọn (ý EasyTool của microsoft/JARVIS)

Đo bằng `reg.router_tool_list` / `json.dumps(reg.openai_tools(...))`:

| Phần | Trước | Sau |
|---|---|---|
| Danh sách tool router chat (chủ bot, 16 tool) | 4.221 | 2.622 |
| Prompt router chat (không lịch sử) | 5.470 | 3.487 |
| JSON tools `/agent` (14 tool) | 4.037 | 3.558 |
| Danh sách JSON-router `/agent` | 2.714 | 1.719 |
| Mẫu JSON-router `/agent` | 810 | 489 |

Giữ các quy ước quyết định độ chính xác (weekdays 0=Thứ Hai, chép nguyên văn
"when", days tính cả hôm nay, không tự điền địa điểm, 2 dạng args âm lịch,
`remember_fact` khác `save_note`, `search_history` chỉ cho chuyện đã nói). Test
router/agent cũ giữ nguyên và pass; `test_tool_catalog_budget` đặt trần tổng
ký tự; golden set tier A (`test/evals`) pass. Tier B (gọi provider thật) chưa
chạy trong đợt này (không có mạng/key) - chủ bot nên chạy `RUN_LIVE_EVALS=1`
một lần sau khi deploy.

### Việc chủ bot cần làm

- Không có migration mới (đề xuất chờ nằm trong bảng `settings`).
- Biến mới (đã có trong `.env.example`, `render.yaml`): `PROMPT_REDACTION=1`,
  `STYLE_RULES_NEED_APPROVAL=1`.
- Lệnh mới `/hoc` đã vào menu Telegram (`bot_app.py`) - menu tự cập nhật khi bot
  khởi động.
- Nên chạy golden set tier B một lần để xác nhận router chọn tool đúng với mô tả
  mới.

### Rủi ro

- Che bí mật là heuristic: sót mật khẩu viết kiểu lạ (không có từ khoá), hoặc
  hiếm khi che nhầm 1 chuỗi ASCII sau "mật khẩu ... là". Tắt bằng `PROMPT_REDACTION=0`.
- Câu hỏi cần chính giá trị bí mật (vd "mật khẩu Abc@2024 có mạnh không") giờ model
  không thấy giá trị - trả lời chung chung.
- Trả lời "ghi nhớ"/"bỏ" bằng chữ dựa vào trạng thái trong RAM: restart giữa chừng
  thì dùng `/hoc ok|bo`.
