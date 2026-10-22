# Đánh giá và sửa 10/2026: tử vi, thần số học, web công khai

Đợt này xem lại ba phần theo bốn bộ tiêu chí (review chất lượng kiểu "người sẽ bị
gọi khi code hỏng", thiết kế API/giao diện lập trình, kỹ thuật giao diện +
khả năng tiếp cận WCAG 2.1 AA, gỡ lỗi có hệ thống + test bảo vệ), rồi sửa
ngay trong cùng nhánh. Kết quả kiểm tra: `ruff check .` sạch, `ruff format`
đúng trên mọi file .py đã sửa, `pytest -q` 1357 passed / 26 skipped (trước
đợt: 1321 passed).

Cách làm: bảng an sao, độ sáng và quy ước thần số học được tra chéo từ nhiều
nguồn độc lập (sách "Tử Vi Đẩu Số Tân Biên" bản PDF, mã nguồn mở `lasotuvi`,
tracuutuvi.com, thayungkhiem.vn, tuvihiendai.vn, tuvi.cohoc.net, hoc.kabala.vn,
thaiam.vn; thần số học: tracuuthansohoc, sách Lê Đỗ Quỳnh Hương / David A.
Phillips, Hans Decoz) và chỉ nhận khi ≥ 2 nguồn khớp; chỗ các trường phái
khác nhau được ghi rõ trong code/YAML. Mỗi finding của review được tái hiện
bằng input cụ thể trước khi sửa và mỗi sửa đổi có test đỏ-khi-hỏng đi kèm.

## Việc cần làm khi deploy

Không có migration, không có biến môi trường mới. Chạy lại `scripts/make_site_logo.py`
chỉ khi đổi ảnh logo gốc (logo web đã được tạo sẵn trong `static/`). Nếu muốn
giữ logo vàng kim cũ thì đổi lại hai dải màu trong script và chạy lại.

## Thay đổi người dùng nhìn thấy

### Tử vi (`/tuvi`, `/tuvi_chitiet`, trang `/tu-vi`, `POST /api/v1/tuvi/charts`)

- **Độ sáng sao** (miếu M / vượng V / đắc Đ / bình B / hãm H) cho 14 chính tinh
  và các phụ tinh thường ghi đắc/hãm (Kình Đà, Hỏa Linh, Không Kiếp, Xương
  Khúc, Hình Riêu, Khốc Hư, Thiên Mã, Song Hao). Bảng 14 chính tinh chép theo
  "Tử Vi Đẩu Số Tân Biên" (Vân Đằng Thái Thứ Lang, 1956) và khớp 168/168 ô với
  ba nguồn độc lập; nằm trong `assets/tu_vi.yaml`, sửa không cần đổi code.
- **Thêm 10 phụ tinh** có trên hầu hết lá số Việt Nam nhưng trước đây thiếu:
  Thiên Quan, Thiên Phúc, Lưu Hà, Thiên Trù (theo can năm), Quốc Ấn, Đường Phù,
  Lưu Niên Văn Tinh (từ Lộc Tồn), Đẩu Quân (chi năm + tháng + giờ), Thiên Không
  (sau Thái Tuế), Giải Thần (cùng Phượng Các). Bảng Lưu Hà theo Thái Thứ Lang
  (bản Việt Viêm Tử khác ở can Đinh/Canh/Tân - ghi trong code).
- **Giờ Tý đầu**: sinh 23h-24h an theo ngày hôm sau (cách phổ biến ở VN); bot
  nhận "23h", web có mục "Tý đầu", API có `isEarlyTy: true`. Ngày hiển thị vẫn
  là ngày đã nhập.
- **Tháng nhuận**: sinh ngày 1-15 an như tháng trước, từ 16 an như tháng sau
  (quy tắc Thái Thứ Lang); trước đây luôn an như tháng chính.
- Hiểu đúng "12h đêm/khuya" (= 0h) và "7:30 pm"; trước đây "12h đêm" ra giờ
  Ngọ, "pm" bị bỏ qua.
- Lá số in Tứ hóa riêng một dòng, sao chính kèm độ sáng; trang web tô viền cung
  Thân, có chú giải độ sáng; "Năm hiện tại" cho AI lấy theo giờ Việt Nam.

### Thần số học (`/tsh`, `/tsh_chitiet`, trang `/than-so-hoc`, `POST /api/v1/numerology/readings`)

- Thêm **số trưởng thành** (giữ số bậc thầy 11/22/33 như Pythagoras chuẩn),
  **4 đỉnh cao** và **4 thử thách** theo mốc tuổi (đỉnh 1 kết thúc ở 36 trừ số
  chủ đạo rút gọn, mỗi giai đoạn sau 9 năm), **biểu đồ tên** và **biểu đồ tổng
  hợp** (ngày sinh + tên), ý nghĩa tháng cá nhân.
- Ghi rõ quy ước hệ phổ biến ở VN (số chủ đạo có 10, không có 1; năm cá nhân
  theo năm dương lịch; Y tính là nguyên âm) ngay trong docstring và cuối trang.
- Họ tên không có chữ cái Latin bị báo lỗi thay vì trả nửa kết quả null.

### Web (giao diện theo mệnh Đại Lâm Mộc)

- Bảng màu mới trong `static/site.css`: xanh rừng là màu chủ đạo (bản mệnh Mộc),
  xanh nước cho hoàng đạo / Thân / thông tin (Thủy sinh Mộc), son chỉ dùng cho
  mùng 1, rằm, ngày lễ và lỗi (Hỏa, dùng ít), nền ngà pha xanh; **không** dùng
  trắng tinh, xám hay vàng kim (Kim khắc Mộc). Logo LCTA tô lại tông xanh (bản
  sáng và tối), favicon nền xanh đêm. Các trang Quyền riêng tư / Điều khoản /
  Xoá dữ liệu dùng cùng bảng màu.
- Mọi cặp chữ/nền đạt WCAG AA ở cả sáng và tối (thấp nhất 4.9:1); viền ô nhập
  ≥ 3:1; mục menu đang xem có gạch dưới, ngày lễ có gạch chân, mùng 1/rằm có
  chấm - không còn thông tin chỉ bằng màu. Menu 5 mục chia đều 5 cột trên màn
  320px. Danh sách giữ vai trò list cho VoiceOver; hộp lỗi tự nhận focus khi
  trang tải lại; nút "AI luận giải" tự khoá sau khi bấm (tránh tốn 2 lượt AI).
- Trang kết quả tử vi hiện độ sáng từng sao, Tứ hóa dạng nhãn, viền cung Thân;
  trang thần số học thêm khối "Các giai đoạn cuộc đời" và biểu đồ tổng hợp.

### API `/api/v1` (chỉ thêm trường, không đổi trường cũ)

- `isLeapMonth` (GET conversions: true/false/1/0; POST tuvi: bool) và `isEarlyTy`
  sai kiểu trả 422 thay vì im lặng coi là "không nhuận"; nhuận chỉ đi với
  `calendar=lunar`.
- `birthDate` của cả hai POST nhận `YYYY-MM-DD` lẫn `DD/MM/YYYY`; `/days/` không
  cần đệm 0; thiếu `birthDate` báo rõ.
- 404/405 dưới `/api/v1` cùng dạng `{"error": {code, message}}`; `/docs`,
  `/openapi.json` tắt (schema sinh ra rỗng, lộ route nội bộ).
- Trường mới: lá số `palaces[].stars[]` (tên, chính/phụ, Tứ hóa, độ sáng),
  `daiHan.fromAge/toAge`, `menhNotes[].name`, `cucElement`, `cucRelation`,
  `isThuan`, `hourText`, `isEarlyTy`, `anMonth`, `brightnessLegend`; thần số học
  `asOf`, `age`, `lifePath.single`, `maturity`, `pinnacles[]`, `challenges[]`,
  `personalMonthDetail`, `nameChart`, `combinedChart`; lịch `weekdayIndex`
  (ở `/days`) và `weekdayName` (ở `/months`).

## Findings của review và cách xử lý

Đánh số chung; mức: **Phải sửa** (sai kết quả, bảo mật, hỏng ở tải thật),
**Nên sửa** (code rủi ro không test, chậm, lặp), **Nên có**.

### Phải sửa

1. **"12h đêm" thành giờ Ngọ, "7:30 pm" thành giờ Thìn** (`services/tuvi.py`,
   hàm `parse`). Điều kiện cộng 12 chỉ xét giờ < 12 và không biết "pm"/"khuya"
   -> an sai toàn bộ lá số. Đã sửa (`_apply_period`), không nhận "am" vì
   "âm" bỏ dấu cũng là "am"; test 7 trường hợp.
2. **`isLeapMonth` sai dạng bị coi là không nhuận** (`web_site.py` GET
   conversions chỉ nhận chuỗi "true", POST tuvi chỉ nhận bool; "1", "yes",
   `"true"` đều im lặng). Người sinh tháng nhuận nhận lá số sai hoàn toàn.
   Đã sửa: kiểm tra tại biên, 422 `INVALID_LEAP_FLAG` / `INVALID_FLAG`.

### Nên sửa

3. **Thiếu 8-9 phụ tinh thông dụng** nhưng chú thích nói "vài phụ tinh hiếm".
   Đã thêm (mục Tử vi ở trên) và sửa chú thích.
4. **Tháng nhuận an như tháng chính** - lệch 1 cung với thầy/app khác cho
   khoảng một nửa người sinh tháng nhuận. Đã sửa theo quy tắc 1-15 / 16+.
5. **Sinh 23h-24h tính cùng ngày**, không nói rõ quy ước. Đã sửa: giờ Tý đầu
   an theo ngày hôm sau ở bot, web, API; ghi rõ trong USAGE và form.
6. **`/lich/hoi` và `/api/v1/calendar/answers` trả 500** với năm ngoài
   1900-2100 ("25/12/9999" -> `dt.date(10000, ...)` trong `next_tet`). Đã sửa:
   chặn năm trong `_next_solar` / `_solar_dates` (`services/calendar_query.py`).
7. **`/thoi-tiet` cho người lạ gọi Open-Meteo không giới hạn tổng số lượt**
   (mỗi địa danh lạ 3-4 request, dùng chung hạn mức với tool thời tiết của
   chat; 15 s mỗi request chiếm kết nối uvicorn). Đã sửa: ngân sách toàn site
   30 địa điểm lạ / 10 phút (`_new_place_allowed`), địa điểm trong danh sách
   không bị tính; hết thì báo và không gọi API.
8. **`site_data.tuvi_chart` lặp `tuvi.format_chart` và dùng 6 tên private**;
   web từng lệch bot sau khi thêm độ sáng; bấm AI an lá số 2 lần. Đã sửa: dict
   dựng trong `tuvi.chart_data` / `numerology.reading_data`, `site_data` chỉ
   kiểm đầu vào; `tuvi_submit` an 1 lần.
9. **`birthDate` hai POST nhận định dạng khác nhau**, docstring sai. Đã sửa
   (`parse_ymd` nhận cả hai, `/days/` nhận `\d{1,2}`).
10. **404/405/413 trả `{"detail"}` khác `{"error"}`** như docstring/README hứa.
    Đã sửa 404/405 bằng exception handler cho `/api/v1/`; 413 do middleware
    (body > 1 MiB) giữ nguyên và ghi rõ trong tài liệu.
11. **`weekday` cùng tên hai kiểu** (`/days` tên, `/months` số). Giữ trường cũ,
    thêm `weekdayIndex` và `weekdayName`.
12. **Test API chưa phủ** tháng nhuận, lịch âm qua POST, dạng lỗi 404/405, tập
    khoá hợp đồng. Đã thêm (`test_api_thang_nhuan_va_dinh_dang_ngay`,
    `test_api_404_405_...`, `test_api_giu_truong_cu_va_truong_moi_song_song`).
13. **`/docs`, `/openapi.json` công khai nhưng schema rỗng**, lộ route nội bộ.
    Đã tắt.
14. **Viền ô nhập 1.8:1** (WCAG 1.4.11 đòi 3:1). Đã tăng `--border-strong`.
15. **Ngày lễ chỉ đánh dấu bằng nền son 1.17:1**, mùng 1/rằm chỉ bằng màu. Đã
    thêm gạch chân (lễ) và chấm (mùng 1/rằm), sửa chú giải.
16. **Mục menu đang xem chỉ khác màu** (tối 1.09:1). Đã thêm gạch dưới.
17. **Thiên Khôi/Việt tuổi Canh** đang ở Ngọ/Dần (như tuổi Tân). Hai phái:
    "Giáp Mậu Canh ngưu dương" (Toàn Thư, tuvisonkhiem) và "Canh Tân phùng mã
    hổ" (Toàn Tập; đa số trang VN hiện nay: thaiam, thanglongdaoquan,
    tuvicaimenh...). Tra chéo xong: giữ cách hiện tại, ghi rõ trong code.
    Cùng đợt tra chéo: **Thân chủ năm Ngọ** đổi từ Hỏa Tinh sang Linh Tinh
    (bản phổ biến; repo trước đây gộp cả Tý và Ngọ thành Hỏa Tinh - biến thể
    thiểu số). Tứ hóa 10 can, Hỏa Linh, vòng Tràng Sinh, Tuần Triệt, Lộc Tồn,
    Mệnh chủ và 30 phụ tinh theo chi/tháng/giờ/ngày đều khớp nguồn.

### Nên có

18. "Năm hiện tại" trong prompt AI lấy theo UTC - đã đổi sang giờ Việt Nam.
19. Số trưởng thành cộng bản rút gọn của số chủ đạo - đã giữ 11/22/33.
20. `_clean_ai_text` sót "##" khi dòng thụt đầu - đã sửa.
21. Tham số chết (`argument` trong `_with_ai`, `request` trong `_ai_reading`),
    `_change_place` kiểm tra đăng nhập lần hai sau khi đọc form - đã bỏ/đảo thứ tự.
22. `personalMonth` không ghi tháng nào, kết quả theo "hôm nay" không có mốc -
    đã thêm `asOf`, `personalMonthDetail`.
23. Họ tên không có chữ Latin trả null im lặng - đã trả 422 `INVALID_NAME`.
24. `daiHan.from/to` (từ khoá Python) và `menhNotes[].star` lệch tên với trường
    mới - đã thêm `fromAge/toAge`, `name` song song.
25. Menu 5 mục không vừa 320px - đã chia lưới 5 cột.
26. Lỗi thời tiết không nối ô nhập, lặp partial - đã dùng `_errors.html` +
    `aria-describedby`.
27. Hộp lỗi không nhận focus khi tải lại (không JS) - đã thêm `autofocus`.
28. `list-style: none` làm VoiceOver mất vai trò danh sách - đã thêm `role="list"`.
29. `aria-label` trên `<p>` bị bỏ qua - đã đổi thành chữ ẩn.
30. Nút AI bấm hai lần tốn hai lượt - đã khoá nút sau khi gửi.
31. Thiếu test bảng độ sáng đủ 14 sao / đúng 12 ký tự - đã thêm.
32. Chiều nghịch (âm nam / dương nữ) của Hỏa Linh, vòng Tràng Sinh, vòng Bác Sĩ
    không có test - đã thêm (dương nam vs dương nữ cùng ngày giờ).
33. Triệt không có test, Tuần chỉ test một tuần Giáp - đã thêm 10 năm.
34. Tứ hóa chỉ test can Canh; Tứ hóa gắn vào phụ tinh (Hữu Bật, Tả Phù, Xương
    Khúc) không test - đã thêm đủ 10 can.
35. API `hourBranch` kiểu bool/float/12, body rỗng hoặc `null` chưa có test - đã thêm.
36. Năm/tháng cá nhân theo giờ Việt Nam không có test fail-when-broken - đã thêm
    (giả lập 17:30 UTC 31/12 = 00:30 1/1 giờ VN).
37. Bot `/tsh` im lặng với họ tên không có chữ Latin trong khi web trả 422 - đã
    dùng chung quy tắc (`numerology.NAME_ERROR`).
38. Chú giải độ sáng không cùng màu với ký hiệu trong cung - đã sửa selector.

## Chưa đổi (cố ý)

- Trang `/admin` giữ bảng màu xanh dương riêng: là dashboard quản trị, không
  thuộc giao diện công khai; đổi sau nếu muốn đồng bộ.
- Thiên Khôi/Việt tuổi Canh (mục 17) giữ theo phái "Canh Tân phùng mã hổ".
- Các phụ tinh hiếm (Lưu niên Văn Tinh, Thiên Khố, Thiên Quý khác phái) vẫn
  chưa an; độ sáng của Tả Hữu, Khôi Việt, Lộc Tồn, Hóa Lộc/Quyền/Khoa không
  ghi vì các nguồn VN không thống nhất.
- Tiểu hạn, lưu niên chưa có (ngoài phạm vi đợt này).
