# Hướng dẫn cho trợ lý AI / người đóng góp

Tài liệu dự án: xem `README.md`. File này chỉ tóm tắt quy ước khi sửa code.

## Kiểm tra trước khi commit

```bash
ruff check .
pytest -q
cd zalo-gateway && npm run check && npm run format:check && npm test
```

Repo cũ chưa `ruff format` toàn bộ; chỉ format file bạn sửa (CI kiểm tra đúng
các file thay đổi). `stock/analysis.py` và `stock/sector.py` được miễn format.

## Quy ước quan trọng

- Deploy: Render free tier, 512 MB RAM, 1 worker uvicorn + gateway Node trong
  cùng container (supervisord). Mọi tính năng mới phải có giới hạn bộ nhớ
  (cache có trần, không giữ file lớn trong RAM).
- Concurrency: `services.concurrency.assistant_turn(key)` khoá theo người
  dùng (`"owner"` cho Telegram/Zoom, `"zalo:<internal_user_id>"` cho Zalo);
  tổng lượt song song bị chặn bởi `MAX_CONCURRENT_TURNS`. Lệnh `/fb_*` chạy
  ngoài khoá này.
- DB: schema chỉ đổi qua `migrations/NNN_*.sql`. Hàm INSERT không idempotent
  dùng `@_with_reconnect(idempotent=False)`.
- Phân tích cổ phiếu: action và mọi con số entry/stop/target/tỷ trọng do
  `stock/policy.py` quyết định; LLM chỉ diễn giải, không được đổi action.
- Secret lưu DB phải qua `core.crypto` (fail closed).
- Không cài package hay chạy lệnh mạng ngoài các lệnh kiểm tra ở trên khi
  chưa hỏi chủ repo.
