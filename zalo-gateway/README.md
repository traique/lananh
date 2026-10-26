# Zalo gateway

Node adapter dùng `zca-js` cho tài khoản B, chạy cùng container với FastAPI.

## Luồng chính

- QR login được điều khiển từ Telegram qua local control server `127.0.0.1:9901`.
- A ghép đôi bằng `/pair <mã>`; không cần cấu hình UID thủ công.
- Private text dùng chung Gemini/stock/memory/tools với Telegram. Tin trả lời
  (trích dẫn) gửi kèm `quote_text` (chữ tin được trích, tối đa 600 ký tự).
- Tin riêng phân luồng theo `data.msgType` (`src/msgtype.ts`):
  - `chat.photo`, `chat.doodle`, `share.file` đuôi jpg/png/webp: đường ảnh
    (`POST /image-prompt`); câu "đã nhận ảnh" gửi TRƯỚC khi tải/xem ảnh;
  - `chat.voice`: tải bản ghi (ưu tiên URL m4a, chỉ CDN Zalo, trần
    `VOICE_MAX_BYTES`, kiểm magic bytes âm thanh) rồi `POST /voice`;
  - `share.file` PDF/DOCX/TXT/MD: kiểm dung lượng khai báo, tải trong trần
    `DOCUMENT_MAX_BYTES`, nội dung phải khớp đuôi, rồi `POST /file`;
  - `chat.video.msg`, `chat.location.new`, file khác: 1 câu "chưa hỗ trợ"
    (tối đa 1 lần/phút mỗi loại); `chat.sticker`, `chat.gif`: im lặng;
  - loại object lạ: chỉ lấy title làm chữ, không bao giờ vào đường ảnh.
  Câu báo do Python dựng (`POST /notice`: giọng Lan Anh, đúng cách gọi riêng,
  người lạ chưa pair -> im lặng); Python lỗi thì chỉ người quen nhận câu trung
  tính. Nhóm/Facebook giữ nguyên danh sách URL như trước.
- Group text chỉ được forward khi group ID nằm trong allowlist Supabase.
- Summary outbox được gửi riêng từ B sang A. Tin gửi lỗi được báo về Python
  (`POST /internal/zalo/outbox/{id}/failed`), thử lại theo backoff và bỏ cuộc sau
  `ZALO_OUTBOX_MAX_ATTEMPTS` lần (báo chủ bot) - không chặn các tin phía sau.
- Tin riêng xử lý tuần tự theo từng người gửi, song song tối đa
  `ZALO_DIRECT_CONCURRENCY` người (mặc định 1 = số chỗ thành viên bên Python,
  `MAX_CONCURRENT_TURNS - OWNER_RESERVED_SLOTS`); hàng đợi đầy thì báo "đang bận"
  cho người gửi.
- Tin chữ liên tiếp của 1 người gộp thành 1 lượt (`src/burst.ts`): chờ
  `ZALO_DIRECT_DEBOUNCE_MS` sau tin cuối (tối đa `..._MAX_MS` / `..._MAX_MESSAGES`
  tin), và tin gõ trong lúc lượt trước còn chạy được gộp vào 1 lượt kế tiếp
  (gửi Python với id tin cuối). Lệnh, link, ảnh, ghi âm, file đẩy phần đang chờ
  đi ngay để giữ thứ tự.
- `/huy` và lệnh chỉ đọc (`/help`, `/start`, `/notes`, `/history`, `/lich`,
  `/tsh`, `/tuvi`, `/kymon`, `/danhgia`, `/memory` và `/nhac` không tham số -
  khớp `services/turn_control.py`) đi "làn nhanh" NGOÀI hàng đợi, timeout 30s,
  dùng được khi lượt dài đang chạy. `/huy` bỏ luôn tin chữ đang chờ gộp.
- Trong lúc chờ Python: "đang soạn tin" (làm mới mỗi 6s, tối đa 30 lần), "đã
  xem" đủ tham số zca-js, reaction 👌 khi tin phải chờ lượt trước - chỉ cho người
  quen, gọi không chờ có timeout; tắt bằng `ZALO_PRESENCE_EVENTS=0`.
- Câu trả lời gửi kèm `styles` zca-js từ Python (đậm, nghiêng, màu % tăng/giảm);
  Zalo từ chối định dạng (lỗi có `error_code`) thì gửi lại đúng 1 lần chữ thuần,
  lỗi mạng giữ cách thử lại cũ (không gửi trùng bản khác).
- Gửi trả lời lỗi thì thử lại 3 lần (1s/3s/8s); lưu tin nhóm lỗi thử lại 2 lần.
- Nghỉ ngẫu nhiên `ZALO_SEND_GAP_MIN_MS`..`MAX_MS` giữa các đoạn và giữa các tin
  outbox; outbox có ngân sách `ZALO_OUTBOX_MAX_PER_HOUR` tin/giờ - vượt thì hoãn
  sang lượt poll sau (tin vẫn chờ trong DB), không bỏ tin nào.
- Bộ đếm hoạt động (tin nhận, tin gộp, làn nhanh, đoạn gửi, rơi về chữ thuần,
  typing/đã xem/reaction, outbox gửi/lỗi/hoãn) trả trong `GET /status` của control
  server; Python hiện ở mục "Giám sát" trên `/admin`.
- Listener Zalo đóng hẳn: gateway tự đăng nhập lại từ phiên đã lưu với backoff
  5s/15s/60s/5 phút (tối đa 10 lần) rồi chuyển sang `login_required` (quét QR lại).
- Trạng thái gateway được báo về Python qua `POST /internal/zalo/gateway-state`
  với body `{account_id, state, detail?}`; `state` là một trong `connected`,
  `reconnecting`, `closed`, `login_required`, `send_failed` để chủ bot được báo.

## Cấu hình

```env
ZALO_ENABLED=true
ZALO_BRIDGE_SECRET=
ZALO_CONTROL_PORT=9901
ZALO_BOT_ACCOUNT_ID=zalo-bot
ZALO_CONTROLLER_ID=
ZALO_GROUP_REFRESH_MS=60000
ZALO_OUTBOX_POLL_MS=15000
ZALO_GROUP_RETENTION_DAYS=30
ZALO_DAILY_SUMMARY_HOUR=9
ZALO_IMAGE_MAX_BYTES=8388608
ZALO_DIRECT_CONCURRENCY=1
ZALO_DIRECT_SENDER_BACKLOG=8
ZALO_DIRECT_DEBOUNCE_MS=2000
ZALO_DIRECT_DEBOUNCE_MAX_MS=6000
ZALO_DIRECT_DEBOUNCE_MAX_MESSAGES=4
ZALO_SEND_GAP_MIN_MS=600
ZALO_SEND_GAP_MAX_MS=1500
ZALO_OUTBOX_MAX_PER_HOUR=60
ZALO_PRESENCE_EVENTS=1
# Dùng chung với Python (trần tải tin thoại / file tài liệu):
VOICE_MAX_BYTES=5242880
DOCUMENT_MAX_BYTES=5242880
ZALO_OUTBOX_MAX_ATTEMPTS=5
# Chỉ đổi khi gateway KHÔNG gọi Python qua loopback (vd chạy container riêng):
ZALO_BRIDGE_ALLOWED_HOSTS=127.0.0.1,::1
ZALO_BRIDGE_ALLOW_FORWARDED=0
```

`ZALO_COOKIE_JSON`, `ZALO_IMEI` và `ZALO_USER_AGENT` là fallback; có thể để trống khi dùng `/zalo`.

## Đăng nhập

1. Bật gateway và redeploy.
2. Telegram: `/zalo`.
3. B quét và xác nhận QR.
4. Telegram: `/zalo` lần nữa để lấy mã.
5. Zalo A → B: `/pair 123456`.

`/zalologout` xóa session B và controller A.

## Bảo mật

- Không public control port.
- `/internal/zalo/*` chỉ nhận request từ `ZALO_BRIDGE_ALLOWED_HOSTS` (mặc định
  loopback) và từ chối request đi qua proxy (`X-Forwarded-For`...) trừ khi
  `ZALO_BRIDGE_ALLOW_FORWARDED=1`; vẫn bắt buộc `ZALO_BRIDGE_SECRET`.
- Không log cookie, IMEI hoặc URL media đầy đủ.
- Chỉ tải media từ allowlist CDN Zalo và kiểm tra lại domain sau redirect.
- Session/controller phải được mã hóa bằng `SETTINGS_ENC_KEY`.
- Chỉ chạy một listener và không mở Zalo Web bằng B.
- `zca-js` không chính thức; sử dụng có rủi ro tài khoản.
