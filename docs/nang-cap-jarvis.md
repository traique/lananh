# Nâng cấp lấy ý tưởng từ các dự án Jarvis

Các việc lấy ý từ isair/jarvis, ethanplusai/jarvis, amanimran786/jarvis-ai,
microsoft/JARVIS. Mỗi gói là 1 nhánh riêng; mục dưới đây là gói P3.

## P3 — Che bí mật, duyệt cách trả lời, mô tả tool gọn

### 1. Che dữ liệu nhạy cảm trước khi gửi provider (ý từ isair/jarvis)

- Vấn đề: `services/memory_guard` chỉ chặn khi LƯU trí nhớ; câu hỏi, lịch sử chat,
  trí nhớ, grounding vẫn gửi nguyên văn cho các provider miễn phí.
- `services/prompt_redaction.py` (`Redactor`) thay phần GIÁ TRỊ bí mật bằng nhãn
  `[đã ẩn: mã OTP]`, `[đã ẩn: mật khẩu]`, `[đã ẩn: số tài khoản]`, `[đã ẩn: số
  thẻ]`... (nhiều giá trị cùng loại thì đánh số `[đã ẩn: mật khẩu 2]`) và bỏ ký tự
  vô hình. Mẫu: `memory_guard.prompt_secret_spans` - cùng từ khoá neo với
  `memory_guard` (OTP, mật khẩu/mk/pass, PIN, STK, CCCD/CMND/hộ chiếu, thẻ kiểm
  Luhn, khoá bí mật, tiền tố API key) nhưng chặt hơn để không ẩn nhầm: giá trị
  phải trông như mật khẩu (không phải "là gì", "15tr", "5000mAh"), "pass" chỉ khi
  liền dấu tách hoặc "pass wifi/mail...", STK/CCCD không nhận số có dấu chấm tiền
  hay đuôi "đồng", thẻ không từ khoá phải viết theo nhóm 4 số.
- Ráp ở chỗ gửi provider: `ai/orchestrator` `chat` (câu hỏi, lịch sử, trí nhớ,
  grounding - trước khi cắt ngân sách), `ask`, `utility_json`, `analyze_image`,
  `analyze_document`; `ai/agent_service` (câu hỏi, lịch sử, trí nhớ, kết quả
  tool). Chữ chép từ tin thoại đi qua `chat` nên cũng được che. Nội dung file ảnh/
  PDF/âm thanh gửi nguyên (không đọc được để che).
- Có ẩn thì model nhận thêm 1 dòng hệ thống: thông tin đã được che, không đoán,
  không nhắc lại, không hỏi người dùng gửi lại.
- DB lưu như cũ. Router tool chat chạy trong `prompt_redaction.scope()` rồi
  `restore_obj` tham số: "ghi chú giúp anh: mật khẩu wifi là ..." vẫn lưu đúng
  ghi chú (provider chỉ thấy nhãn). Harness eval router làm giống hệt.
- Tắt: `PROMPT_REDACTION=0`.

### 2. Bài học cách trả lời phải được duyệt (ý từ amanimran786/jarvis-ai)

- Trước: bộ trích nhớ tự ghi fact `cach_tra_loi` - đoán sai là mọi lượt sau trả
  lời lệch.
- Giờ (`services/style_approval.py`, `STYLE_RULES_NEED_APPROVAL=1` mặc định):
  quy tắc bộ trích xuất đề xuất được so dòng với quy tắc đang áp (thêm/bỏ dòng)
  và lưu ở settings `style_pending:<user_id>` (JSON 1 dòng/người, tối đa 8 dòng
  mỗi loại, hết hạn sau 3 ngày). Đề xuất mới khi còn đề xuất cũ thì gộp; lúc
  duyệt áp diff lên quy tắc HIỆN HÀNH (không đè lời người dùng vừa tự dặn).
- Hỏi 1 lần cho mỗi đề xuất, tối đa 1 lần/ngày/người, bằng tin RIÊNG ngay sau câu
  trả lời chat (không chen giữa câu trả lời): Telegram có nút [✅ Ghi nhớ] [🗑 Bỏ]
  (verb `hoc` trong whitelist `services/action_buttons.py`, chỉ chủ bot bấm);
  Zalo/Zoom nhắn "ghi nhớ"/"bỏ" là tin kế tiếp ngay sau câu hỏi (6 giờ), hoặc
  `/hoc ok|bo` bất cứ lúc nào (mọi kênh; `/hoc` xem đề xuất).
- Bỏ đề xuất: các dòng đó không được đề xuất lại (danh sách có trần 20).
- Lời người dùng tự dặn rõ vẫn áp ngay: `/nho cách trả lời: ...`, câu có mốc lâu
  dài + chuyện trình bày ("từ giờ trả lời ngắn thôi", "lần sau đừng chèn emoji",
  "đừng ... nữa"). Câu chung chung ("trả lời ngắn thôi em") thành đề xuất.
- Mỗi người (Zalo tách riêng, kể cả Zalo của chủ bot) có khoá riêng; `/forget`,
  `/xoadulieu`, `/zaloxoa` xoá luôn đề xuất chờ.
- Sửa kèm: quy tắc cách trả lời giữ mỗi dòng 1 quy tắc (trước bị gộp thành 1 dòng
  do `memory_guard.clean` gộp cả xuống dòng); bản cũ "- A - B" vẫn tách được.

### 3. Mô tả tool ngắn gọn (ý EasyTool của microsoft/JARVIS)

Đo bằng `reg.router_tool_list` / `json.dumps(reg.openai_tools(...))`:

| Phần | Trước | Sau |
|---|---|---|
| Danh sách tool router chat (chủ bot, 16 tool) | 4.221 | 2.622 |
| Prompt router chat (không lịch sử) | 5.470 | 3.487 |
| JSON tools `/agent` (14 tool) | 4.037 | 3.558 |
| Danh sách JSON-router `/agent` | 2.714 | 1.719 |
| Mẫu JSON-router `/agent` | 810 | 489 |

Giữ các quy ước quyết định độ chính xác (weekdays 0=Thứ Hai, chép nguyên văn
"when", days tính cả hôm nay, không tự điền địa điểm, 2 dạng args âm lịch,
`remember_fact` khác `save_note`, `search_history` chỉ cho chuyện đã nói). Test
router/agent cũ giữ nguyên và pass; `test_tool_catalog_budget` đặt trần tổng
ký tự; golden set tier A (`test/evals`) pass. Tier B (gọi provider thật) chưa
chạy trong đợt này (không có mạng/key) - chủ bot nên chạy `RUN_LIVE_EVALS=1`
một lần sau khi deploy.

### Việc chủ bot cần làm

- Không có migration mới (đề xuất chờ nằm trong bảng `settings`).
- Biến mới (đã có trong `.env.example`, `render.yaml`): `PROMPT_REDACTION=1`,
  `STYLE_RULES_NEED_APPROVAL=1`.
- Lệnh mới `/hoc` đã vào menu Telegram (`bot_app.py`) - menu tự cập nhật khi bot
  khởi động.
- Nên chạy golden set tier B một lần để xác nhận router chọn tool đúng với mô tả
  mới.

### Rủi ro

- Che bí mật là heuristic: sót mật khẩu viết kiểu lạ (không có từ khoá), hoặc
  hiếm khi che nhầm 1 chuỗi ASCII sau "mật khẩu ... là". Tắt bằng `PROMPT_REDACTION=0`.
- Câu hỏi cần chính giá trị bí mật (vd "mật khẩu Abc@2024 có mạnh không") giờ model
  không thấy giá trị - trả lời chung chung.
- Trả lời "ghi nhớ"/"bỏ" bằng chữ dựa vào trạng thái trong RAM: restart giữa chừng
  thì dùng `/hoc ok|bo`.
