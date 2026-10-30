# Trang avatar Lan Anh (`/admin/avatar`)

Lan Anh "người iu" qua 1 tấm ảnh chân dung biết chớp mắt, thở, nghiêng đầu, mấp máy
môi theo giọng nói và đổi biểu cảm theo câu trả lời. Chỉ chủ bot đã đăng nhập `/admin`
mới vào được. Mã: `web_avatar.py` (route), `services/avatar_service.py` (biểu cảm, câu
chào, giọng), `services/avatar_photo.py` (ảnh), `templates/avatar.html` (giao diện),
`vendor/mediapipe/` (nhận diện khuôn mặt lúc upload).

## Dùng thế nào

1. Đăng nhập `/admin` → thanh bên "Công cụ web" → **Avatar** (hoặc mở thẳng
   `/admin/avatar`; chưa đăng nhập thì hiện form đăng nhập rồi quay lại đúng trang).
2. Lần đầu bấm **Chọn ảnh**. Trình duyệt tải bộ nhận diện khuôn mặt (~15 MB, chỉ lần
   đầu, sau đó lấy từ cache), dò 478 điểm mặt rồi gửi ảnh + điểm mặt lên máy chủ. Trang
   hằng ngày chỉ đọc điểm đã lưu, không tải lại bộ nhận diện.
3. **Chạm vào ảnh** để nghe câu chào. Trình duyệt (nhất là iPhone) chỉ cho phát tiếng
   sau cú chạm đầu tiên, nên trước đó câu chào chỉ hiện chữ.
4. Gõ tin và gửi: em trả lời bằng chữ và đọc thành tiếng, môi mấp máy theo giọng, mặt
   đổi biểu cảm (vui, thương, ngại, buồn, giận, ngạc nhiên, bình thường).
5. Nút **micro**: nói chuyện rảnh tay - em nghe → gửi → đọc → tự nghe tiếp. Micro tắt
   trong lúc em nói; **chạm ảnh** để ngắt lời; chạm micro lần nữa để thôi. 3 lần liền
   không nghe thấy gì thì em tự tắt rảnh tay.
6. Nút **loa**: tắt/bật giọng đọc (tắt thì máy chủ không tạo giọng, không tốn lượt TTS).
7. Nút **⋯**: chọn giọng + **Nghe thử** - đầu danh sách là **Hoài My** (mặc định), rồi 4
   giọng Gemini gợi ý; đổi ảnh, thêm ảnh mắt nhắm, xoá ảnh.

## Yêu cầu ảnh

- PNG, JPEG hoặc WebP, tối đa 5 MB. Trình duyệt tự xoay đúng chiều và thu về cạnh dài
  1280 px trước khi gửi.
- Nhìn thẳng vào máy, mặt chiếm khoảng 1/3 khung hình (mặt quá nhỏ bị từ chối), miệng
  ngậm tự nhiên, cằm không bị che (tóc, tay, khẩu trang), đủ sáng, không kính râm.
- Ảnh mắt nhắm (tuỳ chọn): chụp cùng khung với ảnh chính (dùng chân máy hoặc tựa máy),
  chỉ nhắm mắt; phải cùng kích thước ảnh chính sau thu nhỏ. Có ảnh này thì lúc chớp mắt
  trang hoà dần sang ảnh mắt nhắm thay vì kéo mí. Đổi ảnh chính khác kích thước thì ảnh
  mắt nhắm cũ bị xoá; xoá ảnh chính thì xoá luôn ảnh mắt nhắm.
- Máy chủ không tin Content-Type: mở lại ảnh bằng Pillow, xoay theo EXIF rồi bỏ
  EXIF/ICC, nền trắng cho ảnh trong suốt, cạnh dài tối đa 1280 px, lưu WebP (~200-350 KB)
  trong bảng `avatar_photo` (migration 029). Điểm mặt phải đúng 478 điểm, toạ độ 0..1.
- Ảnh chỉ phục vụ qua route kiểm phiên admin, `Cache-Control: private, no-store`, không
  bao giờ nằm trong `static/`.

## Não, giọng, lịch sử

- Lượt chat chạy `services.channel_chat_service.run_chat_turn` như Telegram (kênh
  `telegram`, `is_admin=True`) trong khoá lượt chủ bot `assistant_turn("owner")`: chung
  trí nhớ, lịch sử và `/huy` với Telegram/Zoom. Nhắc việc tạo từ avatar gửi về Telegram.
  Kênh vẫn là `telegram` vì ràng buộc DB chỉ nhận telegram/zalo/zoom; bảng số đo lượt
  (`turn_metrics`) ghi kênh `web`, mã lượt `web:avatar:...`.
- Kiểu "người iu" chỉ áp cho lượt từ trang này (`style_hint`), không đổi persona, giọng
  hay `/noi` của Telegram/Zalo/Zoom. Lượt hỏng: câu báo lỗi như Telegram, mặt buồn,
  không đọc giọng.
- Giọng: `voice_reply.web_clip` - giọng và phong cách riêng của trang, trả base64 trong
  JSON. Đọc cả khi `VOICE_REPLY_ENABLED=0`; vẫn che bí mật, bỏ emoji/markdown khi đọc,
  chuẩn hoá số/viết tắt, cắt theo `VOICE_REPLY_MAX_CHARS`. Giọng đọc chạy SAU khi nhả
  khoá lượt.
- **Hoài My** (mặc định): giọng `vi-VN-HoaiMyNeural` của Microsoft Edge TTS qua gói
  [edge-tts](https://github.com/rany2/edge-tts) (LGPLv3), miễn phí, không tính quota TTS.
  MP3 phát thẳng trên trình duyệt (không qua ffmpeg, không ghi file tạm), chung cổng 1
  lượt đọc/lần với giọng Gemini, hết giờ sau `AVATAR_EDGE_TIMEOUT_SEC`. Edge không nhận
  chỉ dẫn phong cách bằng lời, nên giọng nhẹ nhàng hơn nhờ chỉnh tốc độ/cao độ
  (`AVATAR_EDGE_*`). Edge lỗi, hết giờ hoặc trả rỗng thì tự đọc lại bằng giọng Gemini
  (`AVATAR_TTS_VOICE` nếu là giọng Gemini, không thì Achernar) với `AVATAR_VOICE_STYLE`,
  lượt chat vẫn chạy; nghe thử khi đó báo "đang đọc tạm bằng giọng ...". Sau 1 lần lỗi,
  10 phút tiếp theo đọc thẳng bằng giọng Gemini đó, không chờ Edge lại ở từng câu.
  Edge TTS là dịch vụ không chính thức của Microsoft, có thể đổi/chặn bất cứ lúc nào;
  khi lỗi trang tự chuyển sang giọng Gemini.
- Giọng Gemini (Achernar, Vindemiatrix, Leda, Sulafat... - 30 giọng): M4A (không có
  ffmpeg thì WAV), chỉ đọc bằng Gemini Live (`LIVE_MODEL`), tính quota TTS như chủ bot
  Telegram. Live lỗi, đang bận, hết quota hoặc đọc lệch chữ (`LIVE_VOICE_MIN_MATCH`) thì
  câu đó chỉ hiện chữ, không chờ thêm Gemini TTS (`VOICE_ENGINE` chỉ áp cho `/noi`).
  `/noi` và giọng Telegram/Zalo/Zoom không có Hoài My.
- Khẩu hình tính ngay trong trình duyệt theo âm lượng giọng đang phát (mở nhanh, khép
  chậm, có trần); máy chủ không xử lý thêm âm thanh hay ảnh.
- Câu chào soạn sẵn theo khung giờ Việt Nam (sáng/trưa/chiều/tối/khuya), không gọi AI,
  không ghi lịch sử. Giọng đọc câu chào và câu nghe thử giữ trong RAM tối đa 10 clip
  (bản đọc tạm bằng Gemini khi Edge lỗi thì không giữ, lần sau thử lại Edge).
- Nghe: Web Speech API (vi-VN) của trình duyệt. Trình duyệt không có, hoặc dịch vụ nghe
  không chạy (8 giây chưa bắt đầu, lỗi mạng), thì ghi âm và gửi `/admin/api/avatar/stt`
  (Groq Whisper như tin thoại, trần `VOICE_MAX_BYTES`, tối đa 60 giây mỗi lượt nói).

## Biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `AVATAR_STYLE_HINT` | lời dặn trong `web_avatar.py` | Lời dặn "người iu" cho lượt chat từ avatar (xưng em - anh, ngọt ngào, 1-3 câu, không markdown, tình cảm trong sáng). |
| `AVATAR_TTS_VOICE` | trống = `HoaiMy` | Giọng mặc định khi chưa chọn trên trang (`HoaiMy` hoặc tên giọng Gemini). Tên lạ → `VOICE_TTS_VOICE`. Giọng chọn trên trang (settings `avatar:voice`) ưu tiên hơn biến này. Là giọng Gemini thì cũng là giọng đọc lại khi Hoài My lỗi (không thì Achernar). |
| `AVATAR_VOICE_STYLE` | "Giọng nữ trẻ, nhẹ nhàng, ngọt ngào, ấm áp, hơi nũng nịu..." | Chỉ dẫn phong cách đọc cho giọng Gemini; `off` = không gửi phong cách. |
| `AVATAR_EDGE_RATE` | `-6%` | Tốc độ Hoài My (dấu +/- và %). Sai định dạng → mặc định. |
| `AVATAR_EDGE_PITCH` | `+12Hz` | Cao độ Hoài My (dấu +/- và Hz). Sai định dạng → mặc định. |
| `AVATAR_EDGE_VOLUME` | `+0%` | Âm lượng Hoài My (dấu +/- và %). Sai định dạng → mặc định. |
| `AVATAR_EDGE_TIMEOUT_SEC` | `8` | Chờ Edge TTS tối đa (3-60 giây), quá thì đọc lại bằng giọng Gemini. |

Dùng chung với bot: `LIVE_MODEL`, `LIVE_VOICE_MIN_MATCH`, `VOICE_REPLY_MAX_CHARS`,
`VOICE_STT_ENABLED`, `VOICE_MAX_BYTES`, `VOICE_MAX_SECONDS`, `CHAT_TURN_TIMEOUT_SEC`.

## Giới hạn

- Tin nhắn tối đa 2000 ký tự; giọng đọc tối đa `VOICE_REPLY_MAX_CHARS` ký tự (phần dư
  em nhắc "xem trong tin nhắn chữ").
- RAM máy chủ (Render 512 MB): ảnh xử lý tuần tự 1 cái/lần rồi trả heap; cache giọng 10
  clip; nhận diện khuôn mặt chạy hoàn toàn trong trình duyệt. Gói edge-tts (kèm aiohttp,
  ~10 MB) chỉ nạp ở lần đầu đọc bằng Hoài My; MP3 tối đa 2 MB mỗi câu.
- Trình duyệt: Chrome/Edge (Android, máy tính), Safari iOS 16.4+ (bước upload cần
  WebAssembly SIMD), Firefox. iPhone đang gạt im lặng: Safari 17+ vẫn phát giọng (Audio
  Session API), bản cũ hơn phải tắt chế độ im lặng. Không có WebGL thì vẽ bằng Canvas 2D
  (~20 khung/giây).
- Đây là ảnh 2D được biến dạng, không phải video: ảnh mặt nghiêng nhiều hoặc tóc che mặt
  thì chuyển động kém tự nhiên hơn; khoang miệng là lớp tô tối, không có răng.
- `prefers-reduced-motion`: bỏ lắc đầu, thở; vẫn chớp mắt và mấp máy môi.

## Bảo mật

- Mọi `/admin/api/avatar/*` và file MediaPipe qua `web_admin.require_admin`; hết phiên thì
  API trả 403 và trang chuyển về `/admin?next=/admin/avatar`.
- Chống CSRF ngoài cookie SameSite=Lax: POST/PUT dữ liệu chỉ nhận JSON, upload ảnh dùng
  PUT multipart, `/stt` nhận body `audio/*`.
- Giao diện không có handler inline, không nạp script ngoài; chữ hiện bằng `textContent`.

## API

```
GET    /admin/avatar                                   trang (chưa đăng nhập: form đăng nhập)
POST   /admin/api/avatar/chat           {text, voice?: bool} -> {text, mood, audio?, mime?, image?}
POST   /admin/api/avatar/stt            body audio/*         -> {text} | {error, kind}
GET    /admin/api/avatar/greeting       [?id=&audio=1]       -> {id, text, mood, audio?, mime?}
GET    /admin/api/avatar/voices                              -> {voices, current}
PUT    /admin/api/avatar/voice          {voice}              -> {current}
POST   /admin/api/avatar/voice/preview  {voice}              -> {audio, mime, voice}
GET    /admin/api/avatar/photo?kind=main|blink               -> ảnh WebP
PUT    /admin/api/avatar/photo?kind=    multipart image (+ landmarks cho main)
DELETE /admin/api/avatar/photo?kind=                         -> {removed, photo}
GET    /admin/avatar/vendor/<hash>/<tên>                     file MediaPipe (cache 1 năm)
```

## MediaPipe (`vendor/mediapipe/`)

- `@mediapipe/tasks-vision` **0.10.35** (Apache-2.0), chỉ bản SIMD: `vision_bundle.mjs`,
  `vision_wasm_internal.js`, `vision_wasm_internal.wasm` (đúng integrity sha512 của npm).
- Model `face_landmarker.task` float16 v1: BlazeFace short range + Face Mesh V2 +
  Blendshape V2, cả 3 model card ghi Apache-2.0 (link trong `MANIFEST.json`).
- Tổng 15,4 MB. `MANIFEST.json` ghi nguồn, kích thước, sha256 từng file; `LICENSE` là
  bản Apache-2.0. Test `test/test_web_avatar.py` kiểm file khớp manifest.
- Phục vụ tại `/admin/avatar/vendor/<16 ký tự đầu sha256>/<tên>` (chỉ chủ bot,
  `Cache-Control: private, max-age=31536000, immutable`): đổi file là đổi URL, trình
  duyệt tự tải bản mới.

Cập nhật hoặc dựng lại (chạy ở gốc repo; dòng 1.x đổi API, thử kỹ trước khi lên bản lớn):

```bash
VER=0.10.35
TMP=$(mktemp -d)
curl -sSL -o "$TMP/pkg.tgz" "https://registry.npmjs.org/@mediapipe/tasks-vision/-/tasks-vision-$VER.tgz"
# So sha512 với "dist.integrity" của https://registry.npmjs.org/@mediapipe/tasks-vision
python -c "import base64,hashlib,sys; print('sha512-'+base64.b64encode(hashlib.sha512(open(sys.argv[1],'rb').read()).digest()).decode())" "$TMP/pkg.tgz"
tar -xzf "$TMP/pkg.tgz" -C "$TMP"
cp "$TMP/package/vision_bundle.mjs" "$TMP/package/wasm/vision_wasm_internal.js" \
   "$TMP/package/wasm/vision_wasm_internal.wasm" vendor/mediapipe/
curl -sSL -o vendor/mediapipe/face_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
python - <<'EOF'
import hashlib, json, pathlib
folder = pathlib.Path("vendor/mediapipe")
manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
for name, meta in manifest["files"].items():
    data = (folder / name).read_bytes()
    meta.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
(folder / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
EOF
```

Sửa `version`, `tarball`, `integrity` trong `MANIFEST.json` cho khớp bản mới rồi chạy
`pytest -q test/test_web_avatar.py`.

## Kiểm thử

- `test/test_avatar_photo.py` - chuẩn hoá ảnh (EXIF, WebP, cỡ), điểm mặt, ảnh mắt nhắm.
- `test/test_avatar_service.py` - biểu cảm 7 loại (ranh giới từ tiếng Việt), câu chào
  theo giờ VN, cache có trần, chọn giọng (Hoài My mặc định), `web_clip` không raise và
  tính quota; Hoài My qua edge-tts giả (không gọi mạng): đúng giọng/tốc độ/cao độ, MP3,
  không tính quota; Edge lỗi/hết giờ/rỗng/quá trần/thiếu gói -> đọc lại bằng Gemini;
  tham số sai định dạng -> mặc định; `/noi` không nhận Hoài My.
- `test/test_web_avatar.py` - quyền (403, form đăng nhập), tham số lượt chat, lỗi, chép
  lời, upload, file MediaPipe, quy tắc giao diện.
- `test/test_avatar_sql.py` - SQL thật trên PGlite (CI job `sql-pglite`).

Ngoài phạm vi giai đoạn 1: nói chuyện song công thời gian thực (Gemini Live).
