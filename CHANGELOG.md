# Lịch sử phiên bản Lan Anh

Bản mới nhất nằm trên cùng. Bot đọc chính file này (`core/version.py`) để biết số
phiên bản đang chạy (`/status`, `/phienban`) và soạn tin "đã lên bản mới" gửi chủ bot
qua Telegram + Zoom sau mỗi lần deploy.

Quy ước:

- Mỗi PR làm đổi hành vi bot thêm 1 mục mới ở ĐẦU danh sách: dòng `## X.Y.Z - YYYY-MM-DD`
  rồi các dòng `- ...` viết cho người dùng đọc (ngắn, không thuật ngữ code).
- Tăng Y khi có tính năng mới, Z khi chỉ sửa lỗi, X khi thay đổi lớn làm đổi cách dùng cũ.
- Các bản 0.x được đánh số lại sau, gom theo các PR đã merge (#1 - #46).

## 1.4.1 - 2026-11-24

- Tra được mã CEO (C.E.O Group, sàn HNX): "CEO", "giá CEO", "phân tích CEO", "CEO hôm nay
  sao rồi"... trước đây em hiểu nhầm là chữ "CEO" (giám đốc) nên báo chưa tra ra mã.
  Câu kiểu "CEO của công ty X là ai" thì vẫn hiểu là chức danh.
- Tra được mã có chữ số, có nhiều ở sàn HNX: L14, VC3, S99, C69, PC1, HT1...
- Mã sàn HNX/UPCOM hết cảnh lúc tra được lúc không: em kiểm tra mã theo danh sách niêm yết
  của cả ba sàn, không còn phụ thuộc một lần lấy giá có thể lỗi rồi bị nhớ 10 phút.
- "tra mã CEO", "tra HPG" không còn kéo theo giá mã TRA (Traphaco); hỏi "giá TRA" vẫn ra mã này.

## 1.4.0 - 2026-11-23

- Bát Tự mới: `/battu 15/3/1990 9h30 nam` lập tứ trụ theo tiết khí tới phút, tàng can,
  thập thần, đại vận và tiểu vận năm nay (không tốn AI); `/battu_chitiet` để AI luận giải.
- Thước Lỗ Ban: `/loban 80 cửa` cho biết số đo rơi vào cung tốt hay xấu trên thước thông thủy,
  dương trạch, âm phần và các số đo tốt gần nhất.
- Cung phi Bát Trạch: `/cungphi 1990 nữ` cho cung mệnh, nhóm Đông/Tây tứ mệnh và 4 hướng tốt,
  4 hướng xấu.
- Tử vi có thêm vận năm: lưu niên, tiểu hạn, đại hạn, lưu Tứ Hóa và sao lưu; thêm "năm 2027"
  ở cuối lệnh `/tuvi` để xem năm khác. AI luận giải vận năm dựa đúng các số này.
- Lá số rơi vào chỗ các sách tính khác nhau (sinh 23h-24h, sinh tháng nhuận, tuổi Canh, sinh
  trước 1968) có dòng lưu ý riêng để anh biết vì sao có thể khác app khác.
- Thần số học thêm số ngày cá nhân và số nợ nghiệp (sinh ngày 13, 14, 16, 19).
- `/lich` biết 206 lễ hội Việt Nam: hỏi "hội Lim", "lễ hội tháng Giêng ở Bắc Ninh", "tháng này
  có lễ hội gì", "còn mấy ngày tới hội Lim"; xem một ngày cũng hiện lễ hội của ngày đó.
- Trang web có thêm `/lich/le-hoi`: lễ hội cả năm, lọc theo tỉnh.
- Sửa lịch âm ngày 07/05/2054 và 09/04/2062 bị ghi "ngày 0" (đúng là ngày 30 tháng 3 và
  ngày 30 tháng 2); tiết khí của từng ngày giờ tính chính xác hơn (vài ngày giao tiết trước
  đây ghi sai tên tiết, vd 23/10/2019).
- Tra ngày trước 1968 có lưu ý: lịch âm thời đó tính theo múi giờ khác nên có ngày lệch 1
  ngày so với cách tính hiện nay.
- Trang thần số học sửa dòng giải thích tuổi đỉnh cao cho số chủ đạo 22, 33.

## 1.3.0 - 2026-11-18

- Lệnh `/nhomzalo` gõ được từ Telegram, Zoom và Zalo: liệt kê mọi nhóm Zalo bot đang ở
  kèm tên, số thành viên và group ID; nhóm đang theo dõi có ✅, nguồn Facebook có 📣.
- Lọc nhóm theo tên, không cần gõ dấu: `/nhomzalo gia dinh`.
- Thêm nhóm không cần chép group ID: xem `/nhomzalo` rồi gõ `/themnhom 3` (hoặc
  `/fb_themnhom 3`); bỏ trống tên gợi nhớ thì em tự đặt theo tên nhóm.

## 1.2.0 - 2026-11-16

- Bản tin sáng không còn lẫn tin cũ: bỏ tin đăng quá 36 giờ, và không lặp lại tin đã
  có trong bản tin 2 ngày trước.
- Cùng một bài xuất hiện ở hai nguồn chỉ còn 1 lần trong danh sách "đọc bài N".
- Em chọn tin kỹ hơn: bỏ tin đồn, bài PR, khuyến nghị mua bán cổ phiếu, nhận định không
  có số liệu; tin kinh tế tối đa 4/8 tin để còn chỗ cho thời sự và công nghệ.
- Một nguồn RSS lỗi 3 ngày liền (hoặc cả tuần không có tin mới) thì em nhắn báo anh 1
  lần để đổi link. Nguồn chỉ tạm im dịp cuối tuần, Tết thì không báo.
- Phân tích cổ phiếu thôi dùng thư viện vnstock: bản cũ bị nhà phát triển khoá từ 10/11,
  làm mất một phần dữ liệu và chặn cập nhật bot.
- Sàn niêm yết (để làm tròn đúng bước giá), khối ngoại phiên gần nhất và lịch cổ tức,
  đại hội cổ đông giờ lấy thẳng từ VCI. Bỏ mục tin công ty của VCI; tin về mã vẫn lấy
  từ báo.

## 1.1.0 - 2026-10-31

- App Android "Lan Anh Màn hình" cho điện thoại cũ: giờ, lịch âm dương, thời tiết,
  danh sách cổ phiếu theo dõi, giá vàng, giá xăng, tỷ giá và tin tức, tự cập nhật 5 phút.
  Giờ đêm màn hình tự tắt, đưa tay lại gần là sáng lại.
- Lệnh `/app moi <tên>` lấy mã ghép app (hết hạn sau 10 phút), `/app` xem các máy đã
  ghép, `/app xoa <số>` thu hồi máy.

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
