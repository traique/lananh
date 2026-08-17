# Zalo trên Render

Dự án chạy Uvicorn và zca-js trong cùng một Docker Web Service bằng Supervisor để chỉ tiêu thụ một Render instance.

## Rollout

1. Deploy với `ZALO_ENABLED=false` và xác nhận Telegram hoạt động.
2. Tạo `ZALO_BRIDGE_SECRET` và Fernet `SETTINGS_ENC_KEY` hợp lệ.
3. Đặt `ZALO_ENABLED=true`, save Environment và redeploy.
4. Trong Telegram riêng với bot, gửi `/zalo`.
5. B quét QR và bấm xác nhận trên điện thoại.
6. Gửi `/zalo` lần nữa để lấy mã dùng một lần.
7. Từ A nhắn B `/pair <mã>`.
8. Kiểm tra `/nhomzalo`, thêm nhóm rồi thử `/tongket`.

Không cần copy cookie/IMEI thủ công. Session B và UID A được mã hóa trong Supabase, sau restart gateway tự khôi phục.

## Log khỏe

```text
[zalo] control server 127.0.0.1:9901
[zalo] listener started account=...
```

Node có thể báo đang chờ Python và retry `127.0.0.1:10000` trong vài giây đầu. Đây là race startup bình thường nếu sau đó listener khởi động.

## Rollback

Đặt `ZALO_ENABLED=false` và redeploy. Telegram/Python vẫn hoạt động. `/zalologout` dùng để xóa session và controller trước khi tắt nếu cần thu hồi quyền.

## Tài nguyên

- Một tài khoản B, một listener.
- Ảnh A → B tối đa 8 MB, xử lý tuần tự và xóa file tạm.
- Nhóm chỉ thu thập text từ allowlist.
- Không chạy Electron/Chromium hoặc nhiều account Zalo trên Render Free.
