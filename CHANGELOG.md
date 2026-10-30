# Lịch sử phiên bản Lan Anh

Bản mới nhất nằm trên cùng. Bot đọc chính file này (`core/version.py`) để biết số
phiên bản đang chạy (`/status`, `/phienban`) và soạn tin "đã lên bản mới" gửi chủ bot
qua Telegram + Zoom sau mỗi lần deploy.

Quy ước:

- Mỗi PR làm đổi hành vi bot thêm 1 mục mới ở ĐẦU danh sách: dòng `## X.Y.Z - YYYY-MM-DD`
  rồi các dòng `- ...` viết cho người dùng đọc (ngắn, không thuật ngữ code).
- Tăng Y khi có tính năng mới, Z khi chỉ sửa lỗi, X khi thay đổi lớn làm đổi cách dùng cũ.
- Các bản 0.x được đánh số lại sau, gom theo các PR đã merge (#1 - #46).

## 1.0.0 - 2026-10-30

- Câu chuyện đời thường không còn bị kéo sang cổ phiếu: phần "broker chứng khoán" của
  Lan Anh chỉ dùng khi anh hỏi cổ phiếu, đầu tư, vàng, ngoại tệ hay crypto.
- Câu trò chuyện thường gửi AI ngắn hơn khoảng 1/3, đỡ bị giới hạn lượt của nhà cung
  cấp AI miễn phí.
- Bot có số phiên bản và lịch sử cập nhật: xem bằng `/phienban`, `/status` có dòng
  phiên bản.
- Sau mỗi lần deploy bản mới, em nhắn báo anh qua Telegram và Zoom (trong giờ yên lặng
  thì để sáng báo).

## 0.8.0 - 2026-10-30

- Trang `/admin/avatar`: Lan Anh qua ảnh chân dung sống, có giọng nói và rảnh tay.
- Giọng đọc Hoài My (Microsoft Edge), lỗi thì đọc lại bằng Gemini.
- Chữ hiện ngay và chạy dần trong lúc soạn, khẩu hình và chớp mắt tự nhiên hơn.

## 0.7.0 - 2026-10-29

- Hỏi tự nhiên được trả lời bằng code nhiều hơn, sửa ghi nhầm khoản chi.
- Ảnh: ưu tiên provider đọc được ảnh, ghi rõ model nào đã đọc; prompt tạo ảnh dễ chép.
- Bản tin tóm tắt theo nội dung bài, chặn số liệu và chức danh bịa.
- Web LCTA: trang riêng từng ngày lễ, ngày tốt theo tháng, tuổi theo năm sinh, lịch
  vạn niên; gộp URL trùng, chặn bot huấn luyện AI.

## 0.6.0 - 2026-10-27

- Hỏi tự nhiên thay cho lệnh: không cần nhớ ~100 lệnh.
- Xổ số theo tỉnh, giá vàng, giá xăng (cả trên web), Gemini Live làm dự phòng cuối.
- Sửa sau chạy thật: RAM, tin thoại, Zoom, hỏi giá sản phẩm; cài ffmpeg cho `/noi`.
- Web: diễn giải tử vi và Kỳ Môn bằng lời thường, thiết kế lại giao diện, SEO, xác minh
  Google Search Console.

## 0.5.0 - 2026-10-25

- Nâng cấp lớn: tra web bằng chính provider khi tắt Tavily, Kỳ Môn Độn Giáp, trí nhớ
  dài hạn mới, nhắc việc, tin thoại/ảnh/tài liệu, công cụ vận hành.
- Web: dải giờ hoàng đạo gọn trên điện thoại, lá số giữ bàn 4x4.

## 0.4.0 - 2026-10-23

- Web và trang quản trị thiết kế lại giao diện; admin chia tab, web có tab thời tiết.
- Tử vi, thần số học chuẩn hơn; giao diện theo mệnh Đại Lâm Mộc.
- Đăng nhập web cho AI, sửa địa điểm thời tiết, sửa lỗi 503 khi nhiều người vào.

## 0.3.0 - 2026-10-21

- Thời tiết (mặc định TP.HCM), âm lịch và đọc nguyên bài báo trong chat, không tốn AI.
- `/lich` tra tự do, `/tsh` thần số học, `/tuvi` lá số; web lịch, tử vi, thần số học.

## 0.2.0 - 2026-10-17

- Zalo: `/tongket on|off` bật/tắt tổng kết hằng ngày theo nhóm.
- Trang admin cuộn được và bấm tab đúng trên điện thoại.
- Sửa deploy: chạy lại root trong supervisord.

## 0.1.0 - 2026-10-11

- Rà soát và nâng cấp toàn diện: sửa lỗi, bảo mật, độ bền, AI, phân tích cổ phiếu,
  trang quản trị web.
