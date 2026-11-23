# Quy ước trường phái: lịch, tử vi, bát tự, thần số học, phong thủy

Tử vi, bát tự, thần số học, phong thủy không phải một phương pháp duy nhất: sách vở và
phần mềm an một số sao khác nhau, đổi ngày ở giờ khác nhau, tính tháng nhuận khác nhau.
Trang này gom MỌI chỗ như vậy về một nơi (học theo `docs/schools.md` của Nhật Nguyệt):

- mỗi chỗ ghi lựa chọn của repo, nguồn của lựa chọn đó, cách khác và nguồn của cách khác;
- **mặc định không tự đổi**: đổi là đổi hành vi bot, phải có mục CHANGELOG, sửa test khoá
  giá trị và cập nhật trang này;
- lá số rơi vào chỗ các phái khác nhau có **ghi chú riêng ngay trong kết quả** (bot, web,
  API, prompt AI) để người xem biết vì sao có thể khác app/thầy khác.

Bộ ca kiểm độc lập của Nhật Nguyệt (`test/data/nhatnguyet/`, CC BY 4.0) chạy trong test;
chỗ repo chọn khác họ được bỏ qua có chủ đích trong đúng test đó và liệt kê dưới đây ở cột
"Nhật Nguyệt".

Phần khoa học chỉ nằm ở phép tính lịch (ngày sóc, tiết khí). Tử vi, bát tự, thần số học,
phong thủy là quy tắc truyền thống, chưa được kiểm chứng; bot luôn ghi "tham khảo".

## Âm lịch, tiết khí, ngày giờ

| Chỗ khác nhau | Repo chọn | Cách khác | Ghi chú trong kết quả |
|---|---|---|---|
| Múi giờ xét ngày sóc | UTC+7 cho mọi năm (Quyết định 121-CP ngày 8/8/1967) | Trước 1968 (miền Nam tới 1975) lịch in thường theo UTC+8, trước 1929 theo kinh tuyến 116°25′ Đông (H. Aslaksen; thư mục `history/` của Nhật Nguyệt). Nhật Nguyệt cũng dùng UTC+7 cho mọi năm | Có: `/lich` và `/tuvi` ghi ngày âm theo lịch cũ khi khác (`services.lunar.old_calendar_note`) |
| Tiết khí của một ngày | Tiết hiệu lực lúc hết ngày (0h hôm sau): ngày giao tiết mang tên tiết mới, như lịch Hồ Ngọc Đức | Nhật Nguyệt lấy lúc 12h trưa | Không |
| Thời khắc giao tiết | Kinh độ biểu kiến Mặt Trời, VSOP87 rút gọn (Meeus); lệch IMCCE trung bình 7 giây, tối đa 24 giây (1900-2100) | - | - |
| "Tháng X nhuận" ở năm không nhuận tháng X | Báo không có tháng nhuận đó | Nhật Nguyệt coi là tháng X thường | Có (câu báo lỗi) |
| Ngày 30 của tháng thiếu | Báo tháng chỉ có 29 ngày (`find_lunar_date`) | Thuật toán gốc lùi sang mùng 1 tháng sau | Có |
| Thập nhị trực | Tháng theo tiết (Lập xuân = Dần); ngày giao tiết lặp trực hôm trước | - | - |
| Tên thần trực giờ | Huyền Vũ, Câu Trận | Nhật Nguyệt: Nguyên Vũ, Câu Trần | - |

## Lễ hội (bộ dữ liệu le-hoi-viet-nam)

| Chỗ khác nhau | Repo chọn |
|---|---|
| Lễ âm lịch rơi vào năm có tháng nhuận | Tính vào tháng chính, không tính tháng nhuận |
| Lễ ngày 30 ở năm tháng thiếu | Không ghi năm đó (không đoán ngày dời) |
| Lễ do ban tổ chức định ngày (42 lễ) | Không tính ngày, in lịch đã công bố |
| Lễ dương lịch 29/2 | Năm không nhuận thì bỏ |

## Tử Vi

| Chỗ khác nhau | Repo chọn (nguồn) | Cách khác (nguồn) | Ghi chú |
|---|---|---|---|
| Sinh 23h-24h (giờ Tý đầu) | An theo ngày hôm sau (cách phổ biến ở VN, lyso.vn) | Giữ ngày đã nhập (Nhật Nguyệt, một số sách) | Có |
| Sinh tháng nhuận | Ngày 1-15 an như tháng trước, từ 16 như tháng sau (repo ghi theo Thái Thứ Lang; Nhật Nguyệt dẫn Đẩu Số Tinh Thành tr. 20 và `fixLeap` của iztro) | Cả tháng như tháng chính (mặc định Nhật Nguyệt); cả tháng như tháng sau (Toàn Thư) | Có |
| Tứ Hóa can Canh | Lộc Thái Dương, Quyền Vũ Khúc, Khoa Thái Âm, Kỵ Thiên Đồng (Tân Biên bảng 8.23; Toàn Thư) | Khoa Thiên Đồng, Kỵ Thái Âm (Huyền Vi, Thiên Lương, Việt Viêm Tử - mặc định Nhật Nguyệt); Khoa Thiên Phủ (Vương Đình Chi) | Có (năm sinh hoặc năm xem can Canh) |
| Thiên Khôi, Thiên Việt tuổi Canh | Ngọ, Dần ("Canh Tân phùng mã hổ", Toàn Tập; đa số trang VN) | Sửu, Mùi (Toàn Thư) | Trong chú thích chung |
| Hỏa Tinh, Linh Tinh | Dương nam âm nữ: Hỏa thuận Linh nghịch, âm nam dương nữ ngược lại (Tân Biên §8.6.3) | Cả hai thuận (Toàn Thư, iztro); đổi điểm khởi theo giới (Việt Viêm Tử, Quản Xuân Thịnh) | Không |
| Thiên Trù năm Quý | Tuất (Tân Biên §8.32) | Hợi (sách Trung văn, iztro) | Không |
| Thiên Thương, Thiên Sứ | Luôn ở Nô Bộc, Tật Ách (Tân Biên §8.21) | Đảo cho âm nam dương nữ (phái Trung Châu) | Không |
| Lưu Hà | Theo Thái Thứ Lang | Việt Viêm Tử khác ở can Đinh, Canh, Tân | Trong chú thích chung |
| Thân chủ năm Tý, Ngọ | Hỏa Tinh, Linh Tinh (bản phổ biến) | Quản Xuân Thịnh đảo Tý/Ngọ; vài sách gộp cả hai là Hỏa Tinh | Không |
| Độ sáng sao | 5 mức Miếu, Vượng, Đắc, Bình, Hãm theo Tân Biên (1956); 14 chính tinh và 15 phụ tinh | 7 mức (iztro) | Chú giải trên lá số |
| Năm xem mặc định | Năm âm lịch hiện tại (trước Tết vẫn là năm cũ, như tuổi mụ) | - | Có (dòng "Vận năm") |
| Tiểu hạn | Thân Tý Thìn khởi Tuất, Dần Ngọ Tuất khởi Thìn, Tỵ Dậu Sửu khởi Mùi, Hợi Mão Mùi khởi Sửu lúc 1 tuổi; nam thuận, nữ nghịch (như Nhật Nguyệt) | - | - |
| Lưu Tứ Hóa | Theo can năm xem, cùng bảng Tứ Hóa của lá số (Canh theo Tân Biên) | Như dòng Tứ Hóa can Canh ở trên | Có (năm xem can Canh) |
| Sao lưu | 5 sao: Lưu Thái Tuế, Lưu Lộc Tồn, Lưu Kình Dương, Lưu Đà La, Lưu Thiên Mã (như Nhật Nguyệt) | Sao lưu theo đại vận, theo tháng: chưa an | - |
| Nguyệt hạn | Chưa an | Toàn Thư khởi từ cung năm; Tân Biên khởi từ tiểu hạn | - |

## Thần số học

| Chỗ khác nhau | Repo chọn (nguồn) | Nhật Nguyệt |
|---|---|---|
| Số chủ đạo tổng 10 | Giữ 10, không có số chủ đạo 1 (Lê Đỗ Quỳnh Hương, David A. Phillips) | Rút về 1 |
| Số theo tên | Rút gọn từng từ rồi cộng (Hans Decoz, tracuuthansohoc) | Cộng liền cả tên |
| Chữ Y | Nguyên âm khi không kề nguyên âm khác (NGUYEN, HUY: phụ âm; LY: nguyên âm) | Luôn là phụ âm |
| Số thái độ | Rút hẳn về 1-9 | Giữ 11, 22, 33 |
| Tuổi hết đỉnh 1 | 36 trừ số chủ đạo; 10, 11 giữ nguyên, 22, 33 rút gọn (kim tự tháp của các trang VN) | 36 trừ số chủ đạo đã rút về 1 chữ số |
| Đỉnh cao 1, 2 | Rút hẳn về 1-9; đỉnh 3, 4 giữ 11, 22 | Giữ 11, 22, 33 cả bốn đỉnh |
| Thành phần đỉnh cao | Ngày, tháng, năm rút về 1-9 trước khi cộng | Chỉ cộng chữ số |
| Số nợ nghiệp | Chỉ xét ngày sinh 13, 14, 16, 19 | Như repo (có hệ xét thêm các tổng trung gian) |

## Kỳ Môn Độn Giáp

| Chỗ khác nhau | Repo chọn |
|---|---|
| Giờ dùng để lập bàn | Giờ đồng hồ địa phương, không đổi sang giờ mặt trời thật |
| Đổi ngày | Từ 23h là ngày hôm sau (đầu giờ Tý) |
| Định cục | Chiết bổ; không làm trí nhuận, siêu thần |
| Xoay bàn | Chuyển bàn |
