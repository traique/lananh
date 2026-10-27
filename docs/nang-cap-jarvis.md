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

## P4 — Trả lời bằng giọng nói, Nhật ký trên web, ghi ăn uống

### 1. `/noi`: trả lời kèm tin thoại (ý từ ethanplusai/jarvis, isair/jarvis)

- `/noi on|off|auto` theo TỪNG người (settings `voice_reply:<user_id>`, mặc định
  off; `auto` = chỉ đọc khi người dùng vừa gửi tin thoại). Cổng toàn bot
  `VOICE_REPLY_ENABLED=1` (người dùng vẫn phải tự bật). Chủ bot Telegram/Zoom dùng
  chung 1 cài đặt; mỗi Zalo (kể cả Zalo của chủ bot) riêng, xoá cùng `/zaloxoa`.
- Bản chữ luôn gửi như cũ; tin thoại đi SAU bản chữ (Telegram: trước câu hỏi duyệt
  cách trả lời). Đọc câu trả lời trò chuyện và câu trả lời nhanh (thời tiết, giá
  vàng...); KHÔNG đọc lệnh "/", báo cáo cổ phiếu, ảnh. Lỗi TTS/ffmpeg/hết quota/hết
  hạn chót chỉ log - câu chữ không bị ảnh hưởng.
- Lọc chữ trước khi đọc (`speakable_text`): bỏ khối code, dòng bảng, link (giữ chữ
  của link markdown), markdown, emoji, dòng "⚙️ API"; bí mật code đã khôi phục trong
  câu trả lời (vd ghi chú mật khẩu wifi) được che như prompt (`prompt_redaction`)
  trước khi gửi TTS. Trần `VOICE_REPLY_MAX_CHARS`
  (600): dài hơn thì đọc đoạn đầu (cắt ở cuối câu) và nói "Phần còn lại anh xem trong
  tin nhắn chữ nha".
- Tổng hợp giọng: Gemini TTS chính thức qua `google-genai` đã cài (2.11, API
  `generate_content` với `response_modalities=["AUDIO"]` + `SpeechConfig` giọng dựng
  sẵn - tài liệu ai.google.dev ghi `generateContent` vẫn được hỗ trợ đầy đủ). Model
  mặc định `gemini-3.8-flash-tts` (bản ổn định hiện hành, có tiếng Việt), giọng
  `Kore`; đổi bằng `VOICE_TTS_MODEL`/`VOICE_TTS_VOICE` (vd `gemini-3.8-flash-lite-tts`,
  giọng `Aoede`, `Leda`...). Đầu ra PCM 16-bit mono 24 kHz: model 3.8 trả WAV có
  header, model cũ trả PCM thô - code nhận cả hai. Đi `orchestrator.synthesize_speech`:
  api1 → api2, mỗi lượt qua `ai.timeouts.bounded()` (trần 45 giây, trong hạn chót
  CHAT_TURN_TIMEOUT_SEC của lượt; còn < 8 giây thì thôi đọc), ghi `provider_calls`
  stage `tts`, hết quota thì cooldown riêng `tts_api1/2` (không làm chat api1/api2 bị
  coi là hết quota). Tối đa 1 lượt đọc cùng lúc (semaphore, chờ tối đa 10 giây).
- Định dạng, không thêm package/apt:
  - Telegram: có `ffmpeg` trên PATH (`shutil.which`) thì đổi sang OGG/Opus 32 kbps và
    gửi `sendVoice`; không có thì đóng gói WAV bằng module `wave` và gửi
    `sendDocument` (Bot API: `sendVoice` chỉ nhận OGG/Opus, MP3, M4A; `sendAudio` chỉ
    MP3, M4A - WAV chỉ gửi được dạng file).
  - Zalo: zca-js `sendVoice({voiceUrl})` gửi link `voiceUrl`/`m4aUrl` cho app Zalo tự
    tải (gửi HEAD trước để lấy Content-Length). App Zalo phát m4a; WAV không chắc phát
    được nên Zalo CHỈ bật khi có ffmpeg (M4A/AAC 48 kbps). Không có ffmpeg thì `/noi`
    trên Zalo nói rõ "vẫn chỉ gửi chữ".
- Đường Zalo: clip M4A vào kho RAM (`voice_reply.ClipStore`: tối đa 20 file và
  `VOICE_REPLY_STORE_MB` = 8 MB, bỏ file cũ nhất khi đầy), tên file
  `<id>.<hết hạn>.<HMAC-SHA256>.m4a` (khoá ngẫu nhiên theo process; sai chữ ký/hết
  hạn `VOICE_REPLY_URL_TTL_MIN` = 10 phút/bị đẩy khỏi kho → 404). Bridge trả thêm
  trường `voice: {path, url, duration_ms, mime}` (`channels/contracts.ZaloVoice`;
  gateway cũ bỏ qua). Gateway (`zalo-gateway/src/voice.ts`) gửi sau các tin chữ:
  tải clip qua loopback `GET /internal/zalo/tts/<tên>` (bridge secret), tải lên CDN
  Zalo (`uploadAttachment` file .m4a), rồi `sendVoice` link CDN - link bền, người nhận
  mở muộn vẫn nghe. Tải lên lỗi thì `sendVoice` thẳng link công khai
  `GET|HEAD /media/tts/<tên>` (`channels/tts_media.py`, cần `WEBHOOK_BASE_URL` hoặc
  `RENDER_EXTERNAL_URL` https). Mỗi bước timeout 20 giây; lỗi chỉ log + đếm
  `voice_reply_failures` (bảng gateway trên `/admin` hiện số tin thoại đã gửi).
- Thành viên Zalo (role user): mỗi tin thoại tính 1 lượt nặng (user_calls kind `tts`,
  `usage_quota.heavy_available`/`record_heavy`); hết lượt nặng thì chỉ gửi chữ.
  Tin thoại Zalo đi kèm lượt chữ (`/internal/zalo/message`) và lượt tin thoại
  (`/internal/zalo/voice`, cho `auto`).

### 2. Nhật ký trên web (ý từ Memory Viewer của isair/jarvis)

- `/admin` → mục mới "Nhật ký": chọn chủ bot hoặc 1 người Zalo đã ghép, xem ghi chú
  phiên (`user_session_notes`: tiêu đề, ý chính, tóm tắt phiên) gom theo ngày giờ VN
  và tóm tắt dài hạn (`user_memory_summary`), tìm theo từ khoá (ILIKE trong tiêu đề,
  ý chính, tóm tắt; `%`/`_` được escape), sửa/xoá từng ghi chú.
- API: `GET /admin/api/journal?user_id=&q=&page=` (20 ghi chú/trang, tối đa 50 trang,
  từ khoá tối đa 100 ký tự), `POST /admin/api/journal/note` `{user_id, session_key,
  action: edit|delete, title, bullets, summary}`. Cùng đăng nhập/phiên như mọi
  endpoint admin; POST chỉ nhận `Content-Type: application/json` (thêm 1 lớp chống
  CSRF ngoài cookie SameSite=Lax); chỉ nhận `user_id` có trong danh sách người dùng
  của trang. Sửa/xoá chạy trong `memory_service.user_lock` của người đó (bộ trích nhớ
  đang ghi thì trả 409 "thử lại sau"); để trống cả 3 ô = xoá. Xoá ghi chú phiên không
  đụng lịch sử chat.
- Giao diện cùng phong cách các mục khác (thẻ, nút viên thuốc, bảng gập trên điện
  thoại); mọi chữ qua `esc()`; tab tải khi mở và không nằm trong vòng tự làm mới
  (đang sửa không bị đè).

### 3. `/an`: ghi ăn uống (ý từ isair/jarvis nutrition)

- `services/nutrition_service.py`, migration 028 (bảng `meals`: ngày ăn, bữa
  sang/trua/phu/toi/khac, món, số phần, kcal, nguồn kcal table/ai/none, event_key
  chống ghi 2 lần khi tin gửi lại).
- `/an phở bò sáng`, `/an 2 bát cơm, cá kho tối`, `/an hôm qua bún chả trưa`,
  `/an nửa tô phở`, `/an trà sữa x2`: tối đa 6 món/lần (tách bằng `,` `+` "và"
  "với"), số phần bằng số hoặc "nửa/một/hai + đơn vị" ("hải sản", "nấm" không bị đọc
  thành số). Bữa không nói thì đoán theo giờ VN.
- kcal: tra `assets/thuc_pham.yaml` (124 món/cách gọi phổ biến, khẩu phần thường gặp,
  ghi rõ là ước lượng; sửa file không cần đổi code), khớp tên dài nhất, câu có dấu so
  bản còn dấu trước. Món không có trong bảng: ghi tên, không có kcal - KHÔNG gọi AI.
  `NUTRITION_AI_ESTIMATE=1`: món lạ được ước lượng bằng 1 lượt `utility_json` qua
  `bounded()` (20 giây), số ngoài 1-3000 kcal bị bỏ, đánh dấu "AI ước lượng".
- `/an`: hôm nay theo bữa + tổng kcal (ghi rõ số món chưa có kcal); `/an tuan`: 7 ngày
  (tổng/ngày, trung bình ngày có số); `/an xoa <số>`; `/an` và `/noi` không tham số là
  lệnh chỉ đọc (chạy ngoài khoá lượt, gateway đưa làn nhanh). Trần 40 món/24 giờ.
- Câu tự nhiên chỉ khi `NUTRITION_NL=1` (mặc định 0), 2 mẫu rất rõ: "ghi ăn …"/"ghi
  bữa trưa: …" và "<sáng|trưa|chiều|tối> nay|qua [anh/em] ăn …". Tránh lặp lỗi exp-1
  (câu nhắc việc có kèm số tiền bị nuốt thành khoản chi): câu nhờ nhắc/ghi chú/nhớ
  (`write_intent.asks_remind_or_remember`) và câu có số tiền (để chi tiêu ghi) không
  bao giờ là bữa ăn; câu hỏi, kế hoạch ("định", "muốn", "sẽ"), đánh giá ("ngon không")
  đi chat AI; "tối nay ăn lẩu" lúc chưa tới giờ tối là kế hoạch; mẫu "<bữa> nay ăn …"
  đòi MỌI món có trong bảng ("trưa nay anh ăn với khách" vẫn là chat); câu gõ không
  dấu "ghi an …" cũng đòi món có trong bảng ("ghi ân thầy cô"). Chạy sau ghi chi tiêu
  trong chuỗi trả lời nhanh. Tin thoại: câu ghi bữa ăn được báo "chưa ghi, gõ chữ".
- Dữ liệu riêng từng người (Zalo tách Telegram); xoá cùng `/xoadulieu` (cả chủ bot) và
  `/zaloxoa`; `/xuatdulieu` có mục `an_uong` (200 món gần nhất).

### Việc chủ bot cần làm

1. Migration 028 tự áp lúc khởi động; `migrations.lock` đã có dòng 028 (gói P2 dùng
   027 - gộp nhánh thì chạy lại `python -m core.migrations --write-lock`).
2. Biến mới (đã có trong `.env.example`, `render.yaml`, mặc định an toàn):
   `VOICE_REPLY_ENABLED=1`, `VOICE_REPLY_MAX_CHARS=600`,
   `VOICE_TTS_MODEL=gemini-3.8-flash-tts`, `VOICE_TTS_VOICE=Kore`,
   `VOICE_REPLY_URL_TTL_MIN=10`, `VOICE_REPLY_STORE_MB=8`, `NUTRITION_NL=0`,
   `NUTRITION_AI_ESTIMATE=0`.
3. **ffmpeg - quyết định của chủ repo.** Image hiện KHÔNG có ffmpeg: Telegram nhận
   file WAV (bấm mở để nghe), Zalo không có tin thoại. Muốn tin thoại thật trên cả
   2 kênh thì thêm `ffmpeg` vào dòng `apt-get install` của `Dockerfile` (~+80 MB
   image, RAM chỉ khi đổi định dạng, mỗi lần vài chục MB trong <1 giây). Chưa làm vì
   AGENTS.md cấm tự thêm apt.
4. Menu Telegram có thêm `/an`, `/noi` (tự cập nhật lúc khởi động; menu đang 99/100
   lệnh nếu gộp cả P2 - Telegram tối đa 100). Gateway Zalo cần build lại (đã nằm
   trong image).
5. Thử: `/noi on` trên Telegram, nhắn "chào em" → có bản chữ rồi file/tin thoại;
   gửi tin thoại khi `/noi auto`. `/an phở bò sáng`, `/an`, `/an tuan`. Mở `/admin`
   → Nhật ký, sửa 1 ghi chú. Kiểm model TTS dùng được với key AI Studio (lỗi thì log
   "đọc giọng (TTS) lỗi" và `/admin` → lượt gọi stage `tts`).

### Quyết định đã chọn (chủ bot có thể đổi)

- Zalo gửi qua CDN Zalo trước (link bền) thay vì chỉ link công khai 10 phút; link
  công khai là dự phòng.
- Tin thoại không đọc báo cáo cổ phiếu (dài, nhiều số) và lệnh "/". Câu báo lỗi của
  lượt chat Zalo vẫn có thể được đọc (không phân biệt được với câu trả lời).
- Tin thoại thất bại không báo người dùng (chỉ log), để không thêm tin lỗi sau câu
  trả lời đúng. Thành viên Zalo chỉ bị trừ lượt nặng khi tin thoại tạo được.
- Kcal chỉ từ bảng soạn sẵn; không có mục tiêu kcal/ngày, không gợi ý ăn kiêng.

### Cố ý chưa làm

- Zoom: chưa gửi tin thoại (Chatbot API Zoom cần URL công khai cho file, khác luồng
  ảnh hiện có) - `/noi` trên Zoom đổi cài đặt dùng chung với Telegram.
- Chọn giọng riêng từng người, đọc tin chủ động (nhắc việc, bản tin sáng).
- Nhật ký web chưa sửa tóm tắt dài hạn (chỉ xem); muốn quên 1 điều vẫn dùng `/quen`.
- Bữa ăn chưa vào bản tin sáng/`/chuy`.

### Rủi ro

- Model/giọng TTS: tài liệu Gemini hướng dẫn chủ yếu bằng Interactions API (SDK ≥
  2.25), repo dùng `generate_content` của SDK 2.11 đã cài; nếu model 3.8 không nhận
  `generateContent` ở tài khoản này thì đổi `VOICE_TTS_MODEL` sang
  `gemini-3.1-flash-tts-preview`/`gemini-2.5-flash-preview-tts`, hoặc nâng SDK (cần
  chủ repo duyệt). Quota TTS miễn phí thấp: hết quota thì chỉ còn chữ.
- zca-js `uploadAttachment` file "others" chờ sự kiện websocket không có timeout bên
  trong - gateway bọc timeout 20 giây (lời hứa treo vẫn chạy nền tới khi socket trả).
  Chưa thử với Zalo thật trong đợt này (không có mạng/tài khoản).
- Link công khai `/media/tts/` không cần đăng nhập (ai có link trong 10 phút nghe
  được câu trả lời đó) - link chỉ nằm trong tin gửi người nhận.
- Bảng kcal là ước lượng thô (±20-40%); câu tự nhiên dựa trên mẫu cố định.
