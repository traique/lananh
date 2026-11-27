# Zalo gateway

Node adapter dùng `zca-js` cho tài khoản B, chạy cùng container với FastAPI.
Cần Node 22 (`engines >=22`, `.nvmrc`; Dockerfile và CI build bằng Node 22).

## Mã nguồn

- `src/index.ts`: chỉ nối dây (config, hàng đợi, control server, lịch outbox, tín hiệu tắt).
- `src/dispatcher.ts`: xử lý tin (lọc trùng, phân luồng, gộp, làn nhanh, ảnh/thoại/file,
  `/pair`, tin nhóm, gửi trả lời); nhận `{api, bridge, queues, config, groups}` tiêm
  vào - `test-dispatcher.mjs` test bằng fake.
- `src/session.ts`: khôi phục phiên, QR, listener, tự đăng nhập lại, đăng xuất.
- `src/shutdown.ts`: tắt êm khi SIGTERM/SIGINT. `src/outbox.ts`: gửi outbox + lịch poll/kick.

## Luồng chính

- QR login được điều khiển từ Telegram qua local control server `127.0.0.1:9901`.
  Mọi route của control server đòi header `x-zalo-bridge-secret` = `ZALO_BRIDGE_SECRET`
  (so khớp hằng thời gian; thiếu/sai -> 403): bind loopback chưa đủ vì `/logout` xoá phiên
  và `/status` trả mã QR. Python gọi kèm header qua `channels/zalo_gateway_status.py`.
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
- "Người quen" (nhận typing, "đã xem", reaction, câu báo bận/sự cố của gateway)
  là người Python đã trả lời hoặc chủ bot. Python trả `paired: false` cho người
  lạ (mọi endpoint tin riêng và `/notice`) thì gateway quên người đó; `/zaloxoa`
  còn báo thẳng control server `POST /known/forget {"id": ...}` để từ tin kế
  tiếp người bị gỡ pairing không còn thấy dấu hiệu bot đang đọc. Lúc gắn phiên,
  gateway nạp sẵn người đã pair đang hoạt động (`GET /internal/zalo/known-senders/{account_id}`
  -> `{"sender_ids": [...]}`, tối đa 500, lỗi thì bỏ qua) để ngay sau khi khởi động
  lại, Python sập thì người quen vẫn nhận câu báo sự cố thay vì im lặng.
- Group text chỉ được forward khi group ID nằm trong allowlist Supabase. Tin của nhóm
  không theo dõi (kể cả nhóm nguồn Facebook) bị bỏ ngay ở cửa vào, không ghi vào bộ
  nhớ chống trùng 5.000 id (trước đây đẩy id tin riêng ra khỏi bộ nhớ). Danh sách nhóm
  nạp lại khi Python gọi `POST /groups/refresh` (sau `/themnhom`, `/xoanhom`, `/fb_*`),
  dự phòng mỗi `ZALO_GROUP_REFRESH_MS` (mặc định 5 phút).
- Summary outbox được gửi riêng từ B sang A. Tin gửi lỗi được báo về Python
  (`POST /internal/zalo/outbox/{id}/failed`), thử lại theo backoff và bỏ cuộc sau
  `ZALO_OUTBOX_MAX_ATTEMPTS` lần (báo chủ bot) - không chặn các tin phía sau.
  Python gọi `POST /outbox/kick` (control server) ngay khi thêm tin: gateway gộp các
  kick trong 0,5s thành 1 lượt gửi; poll `ZALO_OUTBOX_POLL_MS` (mặc định 90s) chỉ là
  dự phòng khi kick bị lỡ.
- Python bận (`503`, kèm `Retry-After` - media worker đang đọc body khác) hoặc đang
  khởi động lại (`ECONNREFUSED`): `/message`, `/image-prompt`, `/voice`, `/file` thử
  lại tối đa 3 lần, chờ theo `Retry-After` (trần 10s, mặc định 2s/4s), trong cùng hạn
  chót của lượt; Python idempotent theo message_id nên gửi lại an toàn. Ảnh/thoại/file
  gửi dạng Blob chép 1 lần và bản tải về được thả ngay khi gọi, không giữ suốt lượt.
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
  dùng được khi lượt dài đang chạy. `/huy` bỏ luôn tin chữ đang chờ gộp. Có
  trần (`src/fast-lane.ts`): tối đa 4 lệnh chỉ đọc chờ Python cùng lúc (vượt
  thì vào hàng đợi như lệnh thường); `/huy` mỗi người tối đa 1 lần đang chờ,
  tổng tối đa 4 người - vượt thì bỏ (không xếp hàng sau chính lượt cần huỷ).
- Trong lúc chờ Python: "đang soạn tin" (làm mới mỗi 6s, tối đa 30 lần), "đã
  xem" đủ tham số zca-js, reaction 👌 khi tin phải chờ lượt trước - chỉ cho người
  quen, gọi không chờ có timeout; tắt bằng `ZALO_PRESENCE_EVENTS=0`.
- Câu trả lời gửi kèm `styles` zca-js từ Python (đậm, nghiêng, màu % tăng/giảm);
  Zalo từ chối định dạng (lỗi có `error_code`) thì gửi lại đúng 1 lần chữ thuần,
  lỗi mạng giữ cách thử lại cũ (không gửi trùng bản khác).
- Python trả thêm `voice: {path, url, duration_ms, mime}` khi người gửi bật `/noi`
  (`src/voice.ts`): sau các tin chữ, gateway tải clip M4A qua loopback
  `${ZALO_BRIDGE_BASE_URL}/tts/<tên>`, tải lên CDN Zalo (`uploadAttachment`) rồi
  `sendVoice` link CDN; lỗi thì `sendVoice` link công khai ký HMAC `url` (sống 10
  phút). Mỗi bước timeout 20 giây, lỗi chỉ log (đếm `voice_replies` /
  `voice_reply_failures`).
- Gửi trả lời lỗi thì thử lại 3 lần (1s/3s/8s); lưu tin nhóm lỗi thử lại 2 lần.
- Nghỉ ngẫu nhiên `ZALO_SEND_GAP_MIN_MS`..`MAX_MS` giữa các đoạn và giữa các tin
  outbox; outbox có ngân sách `ZALO_OUTBOX_MAX_PER_HOUR` tin/giờ - vượt thì hoãn
  sang lượt poll sau (tin vẫn chờ trong DB), không bỏ tin nào.
- Việc gửi tin tuần tự theo người nhận (`KeyedMutex` trong `src/limits.ts`):
  trả lời làn nhanh, câu báo, outbox không chen vào giữa các đoạn của 1 câu trả
  lời dài cho cùng người (câu trước treo thì câu sau chờ tối đa 60s).
- Bộ đếm hoạt động (tin nhận, tin gộp, làn nhanh, đoạn gửi, rơi về chữ thuần,
  typing/đã xem/reaction, outbox gửi/lỗi/hoãn) trả trong `GET /status` của control
  server; Python hiện ở mục "Giám sát" trên `/admin`.
- Listener Zalo đóng hẳn: gateway tự đăng nhập lại từ phiên đã lưu với backoff
  5s/15s/60s/5 phút (tối đa 10 lần) rồi chuyển sang `login_required` (quét QR lại).
- Khởi động: khôi phục phiên đã lưu; Python/Zalo chưa sẵn sàng thì thử lại ~4,5 phút
  rồi báo chủ bot (`closed`) và giao cho lịch đăng nhập lại ở trên thay vì đứng yên;
  Zalo từ chối phiên đã lưu thì báo `login_required` ngay.
- Tắt (SIGTERM từ supervisord, `stopwaitsecs=15`, hoặc Ctrl+C): dừng listener và lịch
  outbox, tin chữ đang chờ gộp được đẩy vào hàng đợi, chờ hàng đợi tin riêng/làn
  nhanh/việc gửi đang dở tối đa 10s, báo Python số đoạn đã gửi của tin outbox gửi dở
  (`/outbox/{id}/failed` kèm `chunks_sent`, lần sau gửi tiếp từ đoạn kế thay vì đoạn 0),
  rồi thoát. Tín hiệu lần hai thoát ngay.
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
ZALO_GROUP_REFRESH_MS=300000
ZALO_OUTBOX_POLL_MS=90000
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

- Không public control port. Control server chỉ bind `127.0.0.1` và mọi route đòi
  header `x-zalo-bridge-secret` (cùng `ZALO_BRIDGE_SECRET`); không có route health
  không khoá (supervisord và `scripts/run-zalo-gateway.sh` không gọi HTTP).
- `/internal/zalo/*` chỉ nhận request từ `ZALO_BRIDGE_ALLOWED_HOSTS` (mặc định
  loopback) và từ chối request đi qua proxy (`X-Forwarded-For`...) trừ khi
  `ZALO_BRIDGE_ALLOW_FORWARDED=1`; vẫn bắt buộc `ZALO_BRIDGE_SECRET`.
- Không log cookie, IMEI hoặc URL media đầy đủ.
- Chỉ tải media từ allowlist CDN Zalo và kiểm tra lại domain sau redirect.
- Session/controller phải được mã hóa bằng `SETTINGS_ENC_KEY`.
- Chỉ chạy một listener và không mở Zalo Web bằng B.
- `zca-js` không chính thức; sử dụng có rủi ro tài khoản.

## Nâng cấp zca-js

`zca-js` là thư viện không chính thức, gói npm không xuất kiểu dùng được với NodeNext nên
gateway tự khai báo phần mình dùng (`src/zca-js-compat.d.ts`, `src/types.ts` - phần lớn
là `any`): TypeScript KHÔNG bắt được khi zca-js đổi tên hàm hay giá trị enum. Mỗi lần
nâng bản:

1. Đọc changelog/commit của zca-js giữa 2 bản; chú ý `login`, `loginQR`, listener
   (`start`/`stop`, sự kiện `message`, `group_event`, `closed`, `disconnected`, mã
   đóng `retryOnClose`), `sendMessage` (styles, attachments), `sendSeenEvent`,
   `sendTypingEvent`, `addReaction`, `uploadAttachment`, `sendVoice`, `getGroupInfo`
   (`creatorId`, `adminIds`, `gridInfoMap`), `getAllGroups` (`gridVerMap`), `ZaloApiError`.
2. Ghim đúng bản trong `package.json` (không dùng `^`), `npm install` để cập nhật
   `package-lock.json`, chạy `npm audit --omit=dev`.
3. `npm run check && npm test`: `test-zca-js.mjs` nạp zca-js thật (không mạng) và
   khẳng định mọi method/enum gateway dùng còn nguyên và khớp hằng số của gateway
   (`ThreadType`, `LoginQRCallbackEventType`, `DestType.User`, `Reactions.OK`,
   `TextStyle`, `GroupEventType`, `ZaloApiError`). Đỏ thì sửa shim/hằng số trước.
4. Đối chiếu kiểu trong `node_modules/zca-js/dist/**/*.d.ts` với `src/types.ts`
   (`ZaloMessage`, `ZaloSeenParams`, `ZaloReactionTarget`, `ZaloGroupDetail`) và
   `src/msgtype.ts` (`msgType`: `chat.photo`, `chat.voice`, `share.file`...).
5. Đo RAM sau khi nạp (`node --max-old-space-size=96 dist/index.js`, gateway chia 512 MB
   với Python): bản mới phình heap thì cân nhắc ở lại bản cũ.
6. Thử thật trên tài khoản phụ trước khi deploy: đăng nhập QR, khôi phục phiên đã lưu
   (restart gateway), tin chữ/ảnh/thoại/file, `/nhomzalo`, tin nhóm đang theo dõi,
   typing/đã xem/reaction, outbox, rồi `/zalologout`.
