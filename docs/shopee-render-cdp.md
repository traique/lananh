# Shopee trên hai dịch vụ Render Free

## Kết luận từ log và repo browser

Log ngày 01/10/2026 có frame `https://shopee.vn/verify/traffic/error`, dù URL của trang chính vẫn là `https://affiliate.shopee.vn/offer/custom_link`. Đây là bằng chứng browser đã gặp luồng xác minh/chặn truy cập của Shopee. Body rỗng nên code cũ không phát hiện và đợi hết 45 giây rồi báo thiếu ô Custom Link.

Log này không chứng minh nguyên nhân cụ thể là IP Render, cookie hết hạn, dấu hiệu browser tự động hay lý do khác. `is_logged_in=true` trong query cũng không chứng minh phiên đã được Shopee chấp nhận cho thao tác tạo link. Tăng thời gian chờ field không xử lý được frame xác minh.

Đã đọc repo `https://github.com/traique/shopee-web.git` tại commit `d8554da965cf6aaf12e8caec9c58ca702dbf4a1c` (01/10/2026, phiên bản 1.0.2):

- `src/server.js`: proxy HTTP discovery/WebSocket có `CDP_TOKEN`, chỉ cho một client CDP hoạt động.
- `src/chromium.js`: Chromium headless, khởi động khi cần; xóa thư mục profile trước mỗi lần launch.
- Không có UI đăng nhập, VNC/noVNC hoặc nơi lưu Shopee session trong repo browser.
- `lananh` tạo context mới và nạp `storage_state` từ PostgreSQL. Đây vẫn là cách chạy phù hợp với worker này. Không dùng lại default context chưa đăng nhập của Chromium.

Tách browser sang Render thứ hai giúp giảm RAM cho bot; nó không bảo đảm Shopee sẽ chấp nhận browser. Bản sửa này không dùng Shopee API và không cần máy cá nhân mở liên tục.

## Những thay đổi trong bản sửa

Chỉ sửa logic trong `services/shopee_affiliate_browser.py`, thêm test hồi quy và tài liệu:

1. Kiểm tra URL của trang và iframe trước khi tìm field. `/verify/` hoặc `/captcha` trên hostname Shopee báo `ShopeeVerificationRequired` ngay; vẫn kiểm tra thông báo xác minh/đăng nhập trong body của iframe.
2. Đóng context rồi ngắt CDP trước khi dừng driver Playwright, kể cả khi chuyển link lỗi hoặc bị hủy. Việc lưu session vào DB vẫn diễn ra sau batch thành công.
3. Không log endpoint CDP chứa token; lỗi kết nối CDP trả thông báo riêng, không đưa URL/token từ exception vào log/chat.
4. Khi dùng CDP, timeout kết nối có sàn 120 giây để dành thời gian cho Render wake-up và Chromium khởi động. Ngân sách batch cũng tính phần này. Biến `SHOPEE_BROWSER_LAUNCH_BUDGET_SEC` vẫn có thể đặt cao hơn. Sàn này không kéo dài bước chờ xác minh hay tự giải CAPTCHA.

Bản đầy đủ còn sửa lifecycle/idle cleanup của repo `shopee-web`, giới hạn proxy buffer và thêm lockfile/tests. Không đổi các Chromium flags ảnh hưởng SPA/fingerprint. Xem [hướng dẫn bản đầy đủ](render512-fixes.md).

## Cấu hình giữ hai dịch vụ Render

Trên service browser `shopee-web`, giữ:

```dotenv
CDP_TOKEN=<token hiện tại của browser>
CHROME_START_TIMEOUT_MS=30000
```

Trên service bot `lananh`:

```dotenv
SHOPEE_AFFILIATE_AUTO_ENABLED=true
SHOPEE_BROWSER_CDP_URL=https://<browser-service>.onrender.com/cdp/<CDP_TOKEN>
```

Token trong hai cấu hình phải giống nhau. Khi `SHOPEE_BROWSER_CDP_URL` có giá trị, bot kết nối Chromium ở worker; `SHOPEE_BROWSER_ENGINE` không chọn engine cho worker đó. Không cấu hình `SHOPEE_BROWSER_CDP_USE_EXISTING_CONTEXT`: bản sửa cuối cùng không thêm tùy chọn này.

Giữ session Shopee đang được nạp trong `/admin → Shopee Affiliate`. Browser repo không tự đăng nhập; profile `/tmp` cũng không thay thế session trong DB. Không đưa cookie, JSON session hoặc token CDP vào log công khai.

## Kiểm tra sau deploy

1. Mở `https://<browser-service>.onrender.com/healthz`. `running=false` khi chưa có lần gọi CDP là bình thường; health không chứng minh Shopee đang truy cập được.
2. Trong `/admin → Shopee Affiliate`, test một link mới chưa có trong cache. Link đã cache có thể trả kết quả dù browser vẫn bị chặn.
3. Đối chiếu cả hai service logs: worker phải có client connected/disconnected; bot phải lấy được field, bấm tạo link và nhận link affiliate nếu Shopee cho phép.
4. Nếu bot báo `Shopee yêu cầu xác minh truy cập tại /verify/traffic/error`, dừng việc thử lặp lại. Đây là lỗi Shopee xác minh, không phải lỗi selector hoặc thiếu thời gian chờ CDP. Nếu bot báo không kết nối được CDP, kiểm tra worker, endpoint/token và trạng thái bận.
5. Khi chưa chuyển tự động được, dùng fallback có sẵn `/fb_link <post_id> <affiliate_url>`; với nhiều sản phẩm dùng `/fb_link <post_id> <source_url> <affiliate_url>` cho từng link.

Để hoàn tất OTP/CAPTCHA ngay trên cloud mà không dùng máy cá nhân, browser cần một giao diện tương tác từ xa và cách lưu/khôi phục session bên ngoài filesystem tạm. Repo `shopee-web` hiện chưa có hai phần đó. Thêm UI cũng không bảo đảm giải quyết được trang `traffic/error` nếu Shopee tiếp tục từ chối môi trường này; cần xác nhận trang Custom Link mở được trên chính browser/network đó trước.

## Giới hạn của Render Free

Theo Render Docs tại thời điểm kiểm tra, Free web service có thể ngủ sau 15 phút không có inbound HTTP/WebSocket traffic, wake-up mất khoảng một phút, và mất thay đổi filesystem khi ngủ/restart/redeploy. Free không hỗ trợ persistent disk. Session đã lưu vào DB của bot tách khỏi profile tạm của browser, nhưng độ bền còn phụ thuộc database đang dùng.

Quota Free instance-hours là 750 giờ mỗi workspace mỗi tháng. Nếu cả hai service chạy liên tục 24/7 trong cùng workspace thì tổng giờ vượt quota; worker hoạt động khi cần rồi ngủ phù hợp hơn với quota này. Không thể cam kết hai dịch vụ Free luôn chạy liên tục.

Nguồn: https://render.com/docs/free ; https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp ; https://playwright.dev/python/docs/api/class-browser#browser-close .

## Verification

Kết quả của bản sửa đầy đủ: 642 test pass khi chạy cả Python và SQL trên PostgreSQL nhúng; gateway có 3 Node test pass và TypeScript check/build pass; browser có 6 Node test pass. Chi tiết môi trường, phép đo RAM và giới hạn kiểm thử ở [render512-fixes.md](render512-fixes.md).

Chưa kiểm tra live Render/Shopee: không có endpoint worker đang deploy, token, session hay quyền truy cập Render trong phiên làm việc này. Không coi việc test pass là bằng chứng Shopee đã bỏ chặn.
