# Bản sửa đầy đủ cho Render Free 512 MB

> **Đã gỡ phần tự động chuyển link Shopee** (Playwright, browser CDP `shopee-web`, session Shopee trong `/admin`). Link affiliate nay nhập tay bằng `/fb_link <post_id> <affiliate_url>`. Tài liệu này giữ lại phần lịch sử còn đúng; mọi hướng dẫn về browser Shopee đã bị bỏ. Số test và số đo bên dưới là của bản trước khi gỡ.

Mã đã sửa và kiểm thử trong môi trường làm việc; chưa push GitHub hoặc deploy vào Render của bạn.

## Những lỗi đã sửa

| Phát hiện trong review | Thay đổi cuối cùng |
|---|---|
| F1 — Telegram gửi thất bại nhưng reminder bị đánh dấu đã gửi | Callback thật truyền lỗi về scheduler; chỉ đánh dấu sau gửi thành công. Gửi lỗi được nhả claim hoặc chờ lease để thử lại. |
| F2 — Reminder Zalo/Zoom bị gửi qua Telegram | Lưu kênh, địa chỉ người nhận, bot account và Zoom JID. Zalo dùng outbox có dedup theo reminder; chỉ ACK từ gateway mới đánh dấu đã gửi. |
| F3 — Facebook đã tạo bài nhưng GET kiểm tra lỗi dẫn đến tạo trùng | Lưu Post ID ngay sau create, trước readback. Lỗi kiểm tra hiển thị không làm mất ID. Timeout/mất phản hồi khi create thành `UNKNOWN`, không tự tạo lại. |
| F4 — Facebook kẹt `POSTING` sau restart | Claim có token, lease 5 phút và heartbeat 30 giây. `/fb_ok` có thể thu hồi lease hết hạn; target đã bắt đầu create nhưng chưa biết kết quả cần đối soát. |
| F5 — Webhook được ACK rồi mất tác vụ khi process chết | Telegram/Zoom lưu payload vào PostgreSQL inbox trước khi trả 200; lỗi ghi DB trả 503. Hai worker cố định lấy việc, gia hạn lease, retry có backoff và phục hồi việc chưa hoàn tất. |
| F6 — Hai request Zalo trùng nhau cùng thực thi | Khóa theo account/message/kind bao trùm đọc cache, xử lý và lưu response, kể cả lệnh Facebook ngoài assistant lock. Thiết kế này dùng một instance như `render.yaml`. |
| F7 — Kiểm tra link có thể gọi IP nội bộ qua redirect | Dùng chung public URL/DNS guard, kiểm tra từng redirect, tắt redirect tự động và chỉ đọc headers. GET fallback không tải toàn bộ response. |
| F8 — Phân tích cổ phiếu bỏ qua danh mục `/themcp` | Ưu tiên bảng holdings; bán hết/xóa có bản ghi đóng vị thế để facts cũ không làm cổ phiếu hiện lại là đang nắm giữ. Vị thế còn số lượng luôn được ưu tiên. |
| F10 — Test cũ lệch contract | Chuyển test RAG sang async, sửa mock và các assertion tiêu đề/directive/search filter theo contract hiện tại. |

Các sửa bổ sung để vận hành ổn định: ghi chú/reminder có event key tránh tạo thêm khi webhook retry; Facebook group-post có key theo các message nguồn để retry HTTP không tạo thêm queue item; sửa eviction của verification cache chứa tuple ba phần tử; caption mơ hồ do cache cũ được chặn trước khi đăng.

`POSTED` trong queue có nghĩa là Facebook đã tạo bài và đã có ID. Trạng thái public vẫn cần `/fb_check`; không đồng nhất “đã tạo” với “đã public”.

## Tối ưu RAM và concurrency

| Thành phần | Giới hạn/mặc định của bản sửa |
|---|---|
| Python web | 1 Uvicorn worker; tối đa 8 kết nối/tác vụ HTTP cùng lúc; keep-alive 5 giây |
| Telegram/Zoom inbox | 2 worker; backlog nằm trong DB, không tạo một task RAM cho mỗi webhook |
| Request thường | Tối đa 1 MiB, kiểm tra cả body chunked trước khi parse JSON |
| Ảnh Zalo | Tối đa 8 MiB/ảnh; download theo stream và dừng khi vượt giới hạn |
| Bài Facebook | Tối đa 10 ảnh, tổng ảnh giải mã 16 MiB; request JSON tối đa 22 MiB |
| Request media/admin | 1 request lớn tại một thời điểm; busy trả 503 và `Retry-After: 5` |
| Tạo ảnh Agnes | Ảnh tối đa 8 MiB; JSON generation tối đa 12 MiB; kiểm tra cả stream và base64 |
| Node Zalo | Old-space mặc định 96 MiB, semi-space 8 MiB; không phải giới hạn toàn bộ RSS |
| Zalo event queue | Tối đa 64 direct / 128 group đang chạy hoặc chờ; quá tải báo lỗi vào log |
| Facebook buffer của gateway | Tổng ảnh đang gom 16 MiB, tối đa 64 buffer; flush sớm khi đạt giới hạn; giữ buffer và retry khi POST lỗi |
| Outbox polling | Không chồng thêm lượt poll nếu lượt trước còn chạy |
| AI/Vnstock | Render cấu hình concurrency 2 cho chat providers; Vnstock 1; BLAS/OMP 1 thread |
| Cache trong process | Giá, BCTC, sector giới hạn 128 entry; OHLCV/verification 256, có dọn hết hạn |

Một ảnh quá 8 MiB hoặc bài gom ảnh quá giới hạn sẽ bị từ chối/chia batch, thay vì giữ thêm dữ liệu không giới hạn. Event Zalo vượt queue capacity bị từ chối và ghi log; đây không phải hàng đợi bền vững cho lưu lượng Zalo vô hạn. Buffer chưa gửi của gateway vẫn ở RAM nên không tồn tại qua restart. Webhook Telegram/Zoom và outbox đã ghi DB có thể phục hồi.

## Đo RAM và kiểm thử

Đo trong subprocess Python 3.12/Node 24 của môi trường kiểm thử, không có kết nối user/service thật:

| Phép đo | Kết quả |
|---|---:|
| Python nạp `web`, chưa nạp pandas | Peak RSS khoảng 59.7 MiB; một lần đo import 0.44 giây |
| Python sau khi nạp pandas | Peak RSS khoảng 96.4 MiB |
| Python sau khi nạp thêm SDK Vnstock 4.0.9 | Peak RSS khoảng 111.9 MiB; import SDK khoảng 1.36 giây |
| Zalo SDK nạp, chưa đăng nhập | RSS khoảng 55.7 MiB |
| V8 với old-space 96 + semi-space 8 | Heap limit mà V8 báo: 120 MiB trong Node 24 |

Số đo này cho biết chi phí khởi tạo. Không suy ra peak khi đã đăng nhập, phân tích ảnh/cổ phiếu hoặc gửi nhiều tác vụ.

Kết quả:

- Full suite với `TEST_PGLITE=1`: **642 passed** (14.93 giây), gồm 635 test Python và 7 test SQL. Chạy mặc định: **635 passed, 7 skipped**. Bảy integration test cần DB riêng nên mặc định skip; đã chạy cùng các assertion trên PostgreSQL 18.3 nhúng/PGlite: **7 passed**.
- SQL: migrations 001, 002, 004, 005, 007, 008 chạy được; kiểm tra nâng cấp schema cũ, dedup, lease hết hạn, stale token, outbox ACK, đóng vị thế, cache cũ và HTTP retry.
- Gateway: TypeScript `check`/`build` pass; **3 Node tests pass** cho queue/stream limit.
- Resolver pip với toàn bộ `requirements.txt`, bỏ qua package có sẵn: dry-run thành công từ PyPI + kho Vnstock. Đã kiểm tra import Quote/Trading/Company/Listing của SDK 4.0.9.
- Ruff lint pass; các file mới được format. Không reformat toàn bộ file cũ ngoài phạm vi sửa.

PGlite kiểm tra cú pháp và các chuyển trạng thái SQL; không thay thế kiểm thử khóa giữa nhiều connection/instance trên PostgreSQL triển khai thật. Chưa build Docker hoặc chạy live Render. Đã cài thành công Vnstock 4.0.9/Vnai 2.6.2 từ kho tác giả; unit tests vẫn mock các lời gọi mạng và dữ liệu bên ngoài. Đã đối chiếu bản sửa deploy `lananh-main-render-vnstock-fix.zip` và tài liệu chính thức: dùng `vnstock==4.0.9`, `vnai==2.6.2` và bổ sung kho `https://vnstocks.com/api/simple` khi cài. Pin 4.0.7 không còn có trong kho kiểm tra; không chỉ đổi số phiên bản mà bỏ qua index.

Chạy lại:

```bash
python -m pip install --extra-index-url https://vnstocks.com/api/simple -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
npm ci --prefix zalo-gateway
npm run check --prefix zalo-gateway
npm run build --prefix zalo-gateway
node --test zalo-gateway/test-limits.mjs
```

Unit test `web_reader` cần DNS cho các host công khai trong fixtures; nếu môi trường kiểm thử không có DNS ra Internet, thay resolver của fixtures bằng IP công khai, không tắt public-IP guard của production.

Có thể chạy cùng bảy test SQL trên PostgreSQL nhúng, không cần credentials:

```bash
npm ci --prefix test/sql_support
TEST_PGLITE=1 python -m pytest -q test/test_delivery_sql.py
```

Để kiểm tra trên PostgreSQL thật, bảy SQL test dùng schema riêng và tự xóa schema:

```bash
TEST_DATABASE_URL='postgresql://<user>:<password>@<test-db>/<database>' \
  python -m pytest -q test/test_delivery_sql.py
```

Chỉ dùng database kiểm thử rỗng/riêng; không dùng DB production cho test này.

## Lỗi build Vnstock

Đã giữ bản sửa dependency từ lần deploy trước và ghim `vnai` để cài có thể lặp lại. `vnstock`/`vnai` hiện được phân phối qua kho riêng của tác giả; PyPI đơn lẻ trả “from versions: none”. Dockerfile mới và lệnh cài ở trên có `--extra-index-url https://vnstocks.com/api/simple`; các package thông thường vẫn lấy từ PyPI.

Nguồn chính thức: [Vnstock — lịch sử phiên bản 26/09/2026](https://vnstocks.com/docs/tai-lieu/lich-su-phien-ban). Mã gọi Quote/Trading/Listing/Company trong bot vẫn dùng các lớp hiện tại; không chuyển sang facade Vnstock cũ.

## Cập nhật và deploy

### 1. Bot service

Cập nhật mã bot rồi redeploy. Với service đang tồn tại, nhập các biến dưới đây trong Render Environment; chỉ sửa `render.yaml` trong Git chưa chắc tự cập nhật Environment của service đang chạy:

```dotenv
ZALO_NODE_HEAP_MB=96
ROUTER9_MAX_CONCURRENCY=2
GROQ_MAX_CONCURRENCY=2
OPENROUTER_MAX_CONCURRENCY=2
VNSTOCK_MAX_CONCURRENCY=1
STOCK_BACKTEST_ALLOW_ON_RENDER=false
```

Giữ nguyên `DATABASE_URL`, `SETTINGS_ENC_KEY`, Telegram/Zalo/Zoom credentials và các secrets đang dùng. Giữ **1 instance / 1 Python worker**. Khóa Zalo theo message trong process không thay thế lease DB giữa nhiều instance.

Các biến `SHOPEE_*` và service browser `shopee-web` (nếu đã tạo) không còn được dùng; có thể xóa.

### 2. Migration và dữ liệu cũ

Bot tự chạy migration **008_delivery_recovery.sql** và **009_drop_shopee_session.sql** (xóa session Shopee đã mã hóa còn lưu trong bảng `settings`) qua migration runner lúc khởi động. Sao lưu DB trước khi cập nhật schema; theo dõi log `Applied DB migration 008` / `009` và health trước khi thử chức năng.

Migration không xóa reminder, bài Facebook hoặc cache Shopee cũ:

- Reminder âm có user Zalo tương ứng được đổi sang kênh Zalo; scheduler lấy bot account từ session đã lưu nếu bản ghi cũ chưa có account. Reminder Telegram còn giữ địa chỉ cũ.
- Reminder Zoom cũ có ID dương không phân biệt được với Telegram vì schema cũ không lưu kênh. Kiểm tra các reminder còn chờ của Zoom và tạo lại bằng Zoom/cập nhật metadata rõ ràng; không đoán người nhận.
- Webhook cũ đã bị claim nhưng không lưu payload không thể được phục hồi ngược. Migration giữ dedup cũ, không giả rằng đó là việc chưa xử lý.
- Facebook cũ còn `POSTING`: target chưa có ID trở thành `UNKNOWN`; target có ID được giữ là `POSTED`. Lease cũ chưa có metadata có thể được thu hồi bằng `/fb_ok`.
- Cache Shopee cũ có `verification_version=0`, không tự dùng để cho phép đăng. Nhập lại bằng `/fb_link <post_id> <affiliate_url>`; record cũ được giữ để sửa caption. Link nhập tay được lưu với version 1.
- Nếu caption cũ chứa một shortlink bị dùng chung cho nhiều sản phẩm, bot yêu cầu `/fb_sua` để đưa lại link nguồn; không tự đoán sản phẩm nào ứng với từng vị trí.

Sau khi migration 008 đã chạy, không rollback về code cũ khi còn pending delivery/UNKNOWN: code cũ không hiểu các trạng thái và metadata mới.

### 3. Facebook chưa rõ kết quả

Khi request create bị mất phản hồi, mở Page để xác định bài thực tế trước. Nếu có bài, đối soát bằng:

```text
/fb_check 42
/fb_reconcile 42 default <facebook_post_id>
/fb_ok 42
```

`/fb_reconcile` lấy lease và kiểm tra ID nằm trong `published_posts` của Page được chọn rồi lưu ID, không tạo bài mới. Graph readback dùng danh sách tối đa 50 bài gần nhất như code gốc; nếu ID quá cũ/không đọc được, đối soát có thể chưa xác nhận được. Không có lệnh tự reset `UNKNOWN` rồi đăng lại vì chưa biết việc tạo bài đã hoàn tất hay chưa. Khi chắc chắn không có bài, có thể bỏ qua queue item cũ bằng `/fb_boqua` và gửi lại nguồn như một bài mới.

## Kiểm tra sau deploy

1. Health bot trả 200; migration hoàn tất; gateway báo connected nếu bật Zalo.
2. Thử reminder ngắn từ Telegram và từ Zalo/Zoom: đúng kênh, đúng người. Zalo pending outbox phải được ACK trước khi reminder chuyển `sent=true`.
3. Thử webhook có event ID trùng: chỉ một inbox row; xem pending/lease sau restart và chờ worker xử lý. Inbox payload được xóa khi DONE; retention DONE/dedup mặc định 2 ngày.
4. Quan sát Render Memory khi phân tích cổ phiếu và xử lý ảnh; peak lúc làm việc mới quyết định có đủ 512 MB hay không.

Các hệ thống bên ngoài không cung cấp giao dịch chung với DB: nếu tin nhắn đã gửi nhưng process chết ngay trước khi lưu trạng thái/ACK, vẫn có thể gửi lại. Bản sửa giữ việc chưa hoàn tất, chống các lần tạo trùng đã xác định được và giữ `UNKNOWN` cho Facebook; không cam kết exactly-once cho mọi side effect ngoài DB.

## Giới hạn nền tảng

Render Free có thể ngủ sau 15 phút không có inbound traffic, mất dữ liệu filesystem tạm khi sleep/restart/redeploy, và dùng quota 750 instance-hours mỗi workspace/tháng. WebSocket Zalo do SDK tạo từ service ra ngoài không bảo đảm service Free luôn thức. Tối ưu RAM không biến Free thành nền tảng luôn hoạt động 24/7.

Nguồn chính thức: [Render Free](https://render.com/docs/free).
