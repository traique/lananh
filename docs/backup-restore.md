# Sao lưu & khôi phục DB (Supabase free)

Supabase free **không có bản sao lưu tải về được**. Toàn bộ trí nhớ dài hạn, lịch
sử chat, ghi chú, nhắc việc, danh mục cổ phiếu, user Zalo + session Zalo (đã mã
hoá), API key override và hàng chờ Facebook nằm trong 1 DB duy nhất. Workflow
`.github/workflows/backup.yml` sao lưu mỗi đêm trên GitHub Actions - không tốn RAM
hay đĩa nào trên Render.

## Cách hoạt động

- **04:47 sáng (giờ VN) mỗi ngày**: `pg_dump` schema `public` qua **Session Pooler**
  (cổng 5432 - runner GitHub chỉ có IPv4, host trực tiếp của Supabase là IPv6) bằng
  role **chỉ-đọc** `lananh_backup`.
- Bảng chỉ giữ cấu trúc, **bỏ dữ liệu**: `facebook_post_media` (ảnh), `webhook_inbox`,
  `zalo_direct_responses`, `zoom_processed_events` (cache chống trùng), `user_calls`,
  `provider_calls` (thống kê). `prompts`/`results` được giữ (phục vụ `/history`).
- Bản dump đi thẳng qua pipe vào `gpg --symmetric --cipher-algo AES256` với
  `BACKUP_PASSPHRASE` - bản rõ không chạm đĩa. **Repo public**: artifact và log
  ai đăng nhập GitHub cũng tải/xem được, nên chỉ file đã mã hoá được lưu và log
  chỉ in kích thước + sha256.
- Artifact giữ 30 ngày (biến repo `BACKUP_RETENTION_DAYS`).
- Xong thì ghi 1 dòng vào bảng `backup_runs` (migration 023). Bot kiểm tra mỗi
  ngày: bản mới nhất cũ hơn `BACKUP_STALE_DAYS` (mặc định 3) ngày thì báo chủ bot.
  Lý do: GitHub **tự tắt lịch chạy** của repo public sau 60 ngày không có commit.
- **Ngày 2 hằng tháng** (hoặc Run workflow với `drill = true`): diễn tập khôi phục -
  giải mã bản vừa tạo, `pg_restore` vào Postgres tạm, chạy migration runner thật
  (checksum phải khớp), kiểm tra bảng có dữ liệu thì khôi phục xong vẫn có. Đạt thì
  ghi dòng `kind = 'drill'`; /admin hiện lần diễn tập gần nhất.
- Chưa cấu hình secret thì workflow **bỏ qua** (vẫn xanh, có dòng thông báo).

## Thiết lập (1 lần)

1. Deploy bản có migration 023 (bảng `backup_runs`).
2. Supabase → **SQL Editor**, chạy khối dưới (thay mật khẩu bằng chuỗi ngẫu nhiên
   dài, KHÔNG commit mật khẩu vào repo):

```sql
CREATE ROLE lananh_backup WITH LOGIN PASSWORD '<MẬT-KHẨU-NGẪU-NHIÊN>';
GRANT CONNECT ON DATABASE postgres TO lananh_backup;
GRANT USAGE ON SCHEMA public TO lananh_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO lananh_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO lananh_backup;
-- Bảng/sequence do migration tạo sau này (role postgres) cũng đọc được.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT SELECT ON TABLES TO lananh_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO lananh_backup;
-- Nhịp tim: chỉ được GHI THÊM vào backup_runs.
GRANT INSERT ON backup_runs TO lananh_backup;
```

3. Xem major version của server: `SELECT version();`. Nếu khác 17, vào GitHub →
   Settings → Secrets and variables → Actions → **Variables**, thêm
   `BACKUP_PG_MAJOR` = số major (client `pg_dump` phải **>=** server).
4. Cùng trang đó, tab **Secrets**, thêm:
   - `BACKUP_DATABASE_URL` - chuỗi **Session pooler** (Supabase → Connect → Session
     pooler), đổi user thành `lananh_backup.<project-ref>` (Supavisor cần hậu tố
     project ref - đối chiếu đúng định dạng trong hộp thoại Connect):
     `postgresql://lananh_backup.<project-ref>:<mật-khẩu-url-encode>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require`
   - `BACKUP_PASSPHRASE` - chuỗi ngẫu nhiên >= 32 ký tự (vd `openssl rand -base64 32`).
     **Cất vào trình quản lý mật khẩu**: mất passphrase = mọi bản sao lưu vô dụng.
     Dùng chuỗi KHÁC `SETTINGS_ENC_KEY`.
5. Actions → **Backup DB** → **Run workflow** (tick `drill`) để chạy thử ngay.

## Khôi phục

1. GitHub → Actions → Backup DB → chọn lượt chạy → **Artifacts** → tải
   `lananh-db-<run_id>` (cần đăng nhập) và giải nén.
2. Giải mã: `gpg --decrypt --output lananh.dump lananh.dump.gpg` (nhập
   `BACKUP_PASSPHRASE`).
3. Restore (client >= major của server, vd `docker run --rm -it -v "$PWD:/w" postgres:17 bash`):
   - Sang **project Supabase mới/DB trống** (URL session pooler của role `postgres`):

     ```bash
     pg_restore --list lananh.dump | grep -vE " (SCHEMA - public|COMMENT - SCHEMA public) " > toc.list
     pg_restore --dbname="$TARGET_URL" --no-owner --no-privileges --use-list=toc.list lananh.dump
     ```

   - Chỉ lấy lại **1 bảng** bị xoá nhầm: restore vào 1 DB tạm rồi chép dòng cần, vd
     `pg_restore --dbname="$TEMP_URL" --no-owner --no-privileges --table=notes lananh.dump`.
4. Đổi `DATABASE_URL` trên Render sang DB mới rồi deploy; runner tự áp các migration
   mới hơn bản sao lưu.
5. **`SETTINGS_ENC_KEY` không nằm trong bản sao lưu và không nằm trên GitHub.** Các
   giá trị `enc:` trong bảng `settings` (API key override, session Zalo) chỉ giải
   mã được bằng đúng khoá đó - cất khoá ở nơi riêng (trình quản lý mật khẩu).
6. Bảng bỏ dữ liệu (ảnh Facebook, inbox, cache chống trùng, thống kê) khôi phục
   rỗng - đúng thiết kế.

## Xử lý sự cố

- **`server version mismatch`**: đặt biến `BACKUP_PG_MAJOR` đúng major của server.
- **`query would be affected by row-level security policy`**: bảng trong `public`
  đang bật RLS (thường do bấm theo Security Advisor). App kết nối bằng chủ bảng nên
  không cần RLS; hoặc tắt RLS bảng đó, hoặc thêm policy `FOR SELECT TO lananh_backup USING (true)`.
- **`permission denied for table ...`**: bảng tạo trước khi chạy `ALTER DEFAULT PRIVILEGES`
  - chạy lại `GRANT SELECT ON ALL TABLES IN SCHEMA public TO lananh_backup;`.
- **Bot báo "bản sao lưu DB gần nhất đã N ngày trước"**: Actions → Backup DB → bấm
  **Enable workflow** nếu bị tắt, hoặc xem lỗi của lượt chạy gần nhất.
- Bước "Ghi nhịp tim" báo warning: thiếu `GRANT INSERT ON backup_runs TO lananh_backup`.

## Xoay khoá `SETTINGS_ENC_KEY`

Đặt khoá MỚI vào `SETTINGS_ENC_KEY`, khoá CŨ vào `SETTINGS_ENC_KEY_OLD`, deploy.
Lúc khởi động bot tự mã hoá lại mọi giá trị `enc:` còn dùng khoá cũ (tắt bằng
`SETTINGS_AUTO_ROTATE=0`; khi đó bấm **Xoay khoá ngay** trên /admin → Hệ thống) rồi
báo số giá trị còn lại. Khi còn 0 thì xoá `SETTINGS_ENC_KEY_OLD` trên Render. Bản sao
lưu cũ hơn lần xoay vẫn chứa ciphertext khoá cũ - giữ khoá cũ tới khi các bản đó hết
hạn (30 ngày) nếu muốn khôi phục được.
