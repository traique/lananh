# Zalo gateway

Node adapter dùng `zca-js` cho tài khoản B, chạy cùng container với FastAPI.

## Luồng chính

- QR login được điều khiển từ Telegram qua local control server `127.0.0.1:9901`.
- A ghép đôi bằng `/pair <mã>`; không cần cấu hình UID thủ công.
- Private text dùng chung Gemini/stock/memory/tools với Telegram.
- Private image được tải có cookie phiên, giới hạn 8 MB rồi gửi binary sang Python.
- Group text chỉ được forward khi group ID nằm trong allowlist Supabase.
- Summary outbox được gửi riêng từ B sang A. Tin gửi lỗi được báo về Python
  (`POST /internal/zalo/outbox/{id}/failed`), thử lại theo backoff và bỏ cuộc sau
  `ZALO_OUTBOX_MAX_ATTEMPTS` lần (báo chủ bot) - không chặn các tin phía sau.
- Tin riêng xử lý tuần tự theo từng người gửi, song song tối đa
  `ZALO_DIRECT_CONCURRENCY` người; hàng đợi đầy thì báo "đang bận" cho người gửi.
- Gửi trả lời lỗi thì thử lại 3 lần (1s/3s/8s); lưu tin nhóm lỗi thử lại 2 lần.
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
ZALO_DIRECT_CONCURRENCY=2
ZALO_DIRECT_SENDER_BACKLOG=8
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
