# Sửa lỗi sau review (10/2026)

Tóm tắt thay đổi và việc cần làm khi deploy lên Render (512 MB, chạy root như cũ).

## Việc cần làm khi deploy

1. (Tuỳ chọn) Thêm biến `MAX_CONCURRENT_TURNS=2` trên Render; không set thì mặc định 2.
2. Xoá các biến `SHOPEE_*` cũ trên Render nếu còn (không còn được đọc).
3. Không có migration mới; không cần thao tác DB.
4. Gateway Zalo build lại tự động trong Dockerfile như trước.

## Thay đổi

**Concurrency** (`services/concurrency.py`): bỏ khoá toàn cục. `assistant_turn(key)`
khoá theo người dùng (`owner` cho Telegram/Zoom, `zalo:<id>` cho từng tài khoản Zalo)
và giới hạn tổng lượt song song bằng `MAX_CONCURRENT_TURNS`. Một lượt phân tích
cổ phiếu dài không còn chặn người dùng Zalo khác.

**Database** (`core/database.py`):
- `_with_reconnect` reset đúng pool vừa lỗi (trước đây đọc biến global lúc bắt lỗi
  nên có thể đóng nhầm pool mới).
- Các hàm INSERT (`save_prompt`, `add_chat_message`, `add_note`, `add_reminder`…)
  không retry khi mất kết nối giữa chừng, tránh ghi trùng.
- Pool đóng connection rảnh sau 60s (`max_inactive_connection_lifetime`).

**Admin** (`web_admin.py`, tách khỏi `web.py`):
- Kiểm tra session bằng một dependency chung cho `/admin/api/*`.
- Đăng nhập sai 5 lần/15 phút theo IP, hoặc 30 lần tổng → HTTP 429.
- Sửa lỗi 500 khi tài khoản/mật khẩu có ký tự tiếng Việt; `?hours=` sai → 400.
- Không gọi hàm private của client AI nữa (`api_key`, `model_name` công khai).

**Telegram `/help`**: escape toàn bộ `_` cho Markdown legacy (trước đây `<post_id>`,
`<group_id>`… làm chữ in nghiêng lộn xộn hoặc lỗi parse).

**Dọn code chết**: xoá browser automation Shopee (đã ngừng dùng từ migration 009),
`admin.html` thừa ở gốc, file rác `tools/vn_lint/scripts/a`, `AGENTS.md` do vnai sinh
(thay bằng hướng dẫn của dự án). Luồng `/fb_link` nhập tay giữ nguyên.

**Zalo gateway**: tách `index.ts` (15 KB nhồi trên vài dòng) thành `media.ts`,
`text.ts`, `facebook-buffer.ts`, `control-server.ts`, `types.ts`, `index.ts`;
bỏ phần lớn `any`; thêm prettier, `npm test`. Sửa timer thử lại buffer Facebook
bị tạo hai lần khi gửi lỗi.

**Lint/CI**: ruff bật thêm F401/F811/F841 (phát hiện một test bị dán đè, đã khôi
phục thành test riêng). Thêm `.github/workflows/ci.yml`.

## Chưa đổi (cố ý)

- Container vẫn chạy root.
- Cột `telegram_user_id` vẫn chứa cả id nội bộ của user Zalo; đổi tên cần migration
  và sửa nhiều truy vấn, lợi ích không tương xứng rủi ro.
- `handlers/commands.py` chưa tách nhỏ: nhiều test monkeypatch trực tiếp vào module
  này, tách cần làm cẩn thận ở một lần riêng.

## Đợt 2: luồng Zalo → hàng chờ Facebook

**Bộ lọc bỏ sót** (`services/facebook_caption.py`, gateway):
- Nhận giá dạng "159k", "1tr2", "1.299.000"; không nhầm điều kiện mã ("đơn 0đ", "max 30k",
  "hoàn xu") là giá. Link Shopee trỏ thẳng tới sản phẩm luôn được giữ.
- Cửa sổ gộp ảnh + caption 8s → 30s (`ZALO_FB_MERGE_WINDOW_MS`), kèm luật tự tách khi đăng liên
  tục nhiều sản phẩm. Gửi bài cũ lỗi lúc tách không làm mất tin mới.
- Ảnh nhóm tải lỗi được thử lại 1 lần.
- Lệnh `/fb_boloc [reset]`: thống kê giữ/bỏ/trùng theo lý do.

**Chống trùng** (`services/facebook_dedup.py`, migration 011, bảng `facebook_post_fingerprints`).

**Supabase**: nén ảnh trước khi lưu, xoá ảnh khi đăng xong/bỏ qua, trần 150 bài chờ
(`FACEBOOK_MAX_PENDING`), dọn lịch sử sau `FACEBOOK_HISTORY_DAYS` ngày.

**AI viết lại**: prompt giọng người thật, kiểm tra giữ đúng con số (hỏi lại 1 lần), câu dẫn và
câu mở bình luận đổi theo từng bài.

**Test**: sửa bộ giả lập PGlite (import và tham số bytea) để chạy test SQL thật; CI chạy thêm
`TEST_PGLITE=1 pytest test/test_delivery_sql.py`.

Khi deploy: migration 011 tự chạy lúc khởi động (tạo bảng dấu vân tay và xoá ảnh của bài đã
đăng/bỏ qua còn sót). Nên sao lưu DB trước. Các biến mới đều có mặc định, không bắt buộc set.

## Đợt 3: hiệu suất Render free (chỉ các thay đổi rủi ro thấp)

- Tắt access log của uvicorn (`--no-access-log`): bớt I/O và CPU cho mỗi request. Lỗi ứng dụng
  vẫn được ghi log như cũ.
- HTTP client dùng chung (`services/http_client.py`) cho Facebook Graph, Zoom, DNSE, QuickChart:
  giữ kết nối keep-alive thay vì bắt tay TLS lại mỗi lần gọi. Timeout/header của từng nơi gọi giữ
  nguyên. Link do người dùng nhập (/gia) vẫn đi qua lớp chống SSRF riêng.
- `GET /admin/api/memory-usage` (cần đăng nhập /admin): RAM container theo cgroup (con số Render
  dùng để kill khi vượt 512 MB), RSS từng tiến trình (Python, Node gateway), đỉnh RSS, các thư viện
  nặng đã nạp.
- Job tự `VACUUM FULL facebook_post_media` có điều kiện (xem README) và `GET /admin/api/db-usage`.

Không làm (theo yêu cầu, vì rủi ro): bỏ SDK google-genai, bỏ vnstock, giới hạn riêng phân tích cổ
phiếu, giảm heap Node.

## Đợt 4: Page chứng khoán (`services/market_page.py`)

- Ngày nghỉ lễ không đăng lại phiên cũ (dữ liệu DNSE phải là phiên hôm nay; chưa có thì thử lại).
- Mỗi phiên chỉ đăng 1 lần, kể cả đăng tay rồi lịch chạy (khoá `market_page:stock:session:<ngày>`).
- Sửa định dạng khối lượng > 1 tỷ cổ phiếu ("1,234,5" → "1.234,5").
- Bộ lọc khuyến nghị bắt câu "mềm"; AI được viết lại 1 lần trước khi bỏ bài.
- Lịch chạy bù sau restart và thử lại khi lỗi (thay vòng lặp "ngủ tới giờ" cũ).
- Bản tin chỉ dùng tin 24 giờ qua, bỏ nếu trùng phần lớn bản tin trước.
- RSI theo Wilder, độ rộng ghi rõ là nhóm theo dõi, biểu đồ dd/mm.
- Ảnh biểu đồ và ảnh bài CafeF gắn khung + logo như luồng Zalo; `MARKET_NEWS_IMAGE=none` để tắt
  ảnh CafeF. Comment chỉ tóm tắt 80-120 từ kèm link bài gốc.
