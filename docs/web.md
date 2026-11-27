# Web LCTA, app Android và trang quản trị

Trang web công khai (lịch, thời tiết, tử vi, thần số học, Kỳ Môn, xổ số, giá vàng, giá xăng), app Android màn hình thông tin, trang `/admin` và các trang Meta App.

Tài liệu chính: [README.md](../README.md).

## Web LCTA (lịch, thời tiết, tử vi, thần số học, Kỳ Môn, xổ số, giá vàng, giá xăng)

`web_site.py` - trang render sẵn ở server (chạy được khi tắt JavaScript). Xem
lịch, thời tiết, tử vi, thần số học, Kỳ Môn, xổ số, giá vàng, giá xăng KHÔNG cần đăng
nhập và không tốn AI.
Cần đăng nhập bằng tài khoản admin (`ADMIN_USER`/`ADMIN_PASS`, chung phiên với
`/admin`) cho việc tốn quota hoặc đổi dữ liệu chung:
- nút "AI luận giải" ở trang kết quả tử vi/thần số học và ô câu hỏi ở `/ky-mon`
  (1 lượt AI, chạy dưới khoá lượt của chủ bot như chat, có telemetry kênh `web`);
- thêm/xoá địa điểm hay xem ở `/thoi-tiet` (lưu trong bảng `settings`, key
  `web_weather_places`, tối đa 16 mục, cache RAM - `services/weather_places.py`).

Vị trí hiện tại ở `/thoi-tiet` thì KHÔNG cần đăng nhập: nút "Lấy vị trí hiện tại" (chỉ
hiện khi trình duyệt có JavaScript + định vị và trang chạy HTTPS) hỏi quyền vị trí, làm
tròn toạ độ 2 số lẻ (~1 km) ngay trong trình duyệt rồi `POST /thoi-tiet/vi-tri` lưu vào
cookie `lcta_vitri` của chính trình duyệt đó (Path=/thoi-tiet, HttpOnly, Secure, 1 năm).
Máy chủ không lưu gì (không DB). Mở `/thoi-tiet` không nhập địa điểm thì xem ở vị trí đã
lưu (chip "Vị trí của tôi"), gõ địa điểm khác vẫn xem bình thường; "Xoá vị trí đã lưu"
(`POST /thoi-tiet/vi-tri/xoa`) về lại mặc định TP.HCM. Toạ độ chưa có trong cache dự báo
tính vào cùng hạn mức tra địa điểm mới theo IP (cookie sửa tay được).

Tên nơi ("Phường 1, Đà Lạt") tra MỘT lần lúc lưu, qua Nominatim của OpenStreetMap
(`weather.reverse_geocode`), rồi cất luôn trong cookie (percent-encode) nên xem lại không
gọi Nominatim. Theo chính sách dùng của OSMF: tối đa 1 lượt/giây cho cả tiến trình (khoá
+ giãn cách), User-Agent riêng `LCTA-thoi-tiet/1.0 (+SITE_BASE_URL)`, cache có trần 256 ô
~1 km, lỗi/429/bị chặn thì nghỉ 10 phút; dòng nguồn của bảng dự báo ghi "tên địa điểm © OpenStreetMap"
(dữ liệu ODbL). Tên là phần phụ: hạn chót 6 giây, hết lượt tra địa điểm mới của IP, lỗi
hay `NOMINATIM_ENABLED=0` thì vẫn lưu, trang ghi toạ độ thay tên. Toạ độ đã làm tròn
(~1 km) là thứ duy nhất gửi tới OpenStreetMap; trang `/privacy` (mục "Vị trí ở trang thời tiết") nói điều này.

Trang công khai KHÔNG có nút hay câu nào về đăng nhập: khách chỉ thấy phần tính bằng
thuật toán, khối "Luận giải bằng AI" (tử vi, thần số học, Kỳ Môn) và ô thêm/xoá địa điểm
thời tiết ẩn hẳn. Chủ trang đăng nhập ở `/admin` (trang đăng nhập ghi rõ chỉ dành cho chủ
trang, có link "← Quay lại trang đang xem"); cùng phiên đó quay lại web mới thấy phần AI,
nút sửa địa điểm và "Quản trị"/"Đăng xuất" ở thanh bên. Menu admin có nhóm "Công cụ web"
sang các trang này.

Giao diện dạng ứng dụng (thanh bên trái nhóm Lịch / Tra cứu, cột nội dung 1100px
lưới 12 cột), dựng theo skill ui-craft: khối nội dung tông xanh lá xám (sage) trên
nền trắng, cách nhau bằng bậc tông - không bóng, không gradient, không kính. Một màu
nhấn xanh rừng (mệnh Đại Lâm Mộc) chỉ đặt trên dữ liệu: giờ hoàng đạo, hôm nay,
cung Mệnh, số chủ đạo; nút chính xanh đen; đỏ chỉ cho lỗi, độ sáng hãm. Chữ Be
Vietnam Pro (Google Fonts, có subset tiếng Việt), tiêu đề ngày ở trang chủ dùng
Newsreader. Trang chủ (thiết kế lại theo Hallmark, dạng màn ứng dụng, giữ hệ màu/chữ của
site; dấu Hallmark ở đầu `static/site.css`) lấy ngày hôm nay làm tiêu đề, ô "Hỏi lịch" là
hành động chính bên phải; dưới là dải giờ hoàng đạo (khối sage), hàng 4 số liệu ngày tách
bằng vạch mảnh, dải 3 ngày lễ tới, rồi danh sách "Tra cứu" dạng hàng kẻ vạch (thần số học
có ô ngày sinh ngay trong hàng; tử vi, Kỳ Môn, giá vàng, giá xăng có nút đi tới). Câu trả
lời "giờ hoàng đạo kế tiếp" nằm trên dải 12 giờ địa chi: giờ hoàng đạo nền xanh nhạt, chỉ một ô tô đậm (giờ hiện
tại nếu tốt, không thì giờ tốt kế tiếp), giờ đã qua lùi xuống, giờ hiện tại có vòng ngoài;
từ 23h dải chuyển sang ngày hôm sau (ngày âm đổi ở giờ Tý). Dải tự gập 4 cột x 3 hàng khi
khối hẹp (container query). Các màn công cụ (thiết kế lại cùng hệ đó): form ngắn (xổ số,
Kỳ Môn, đổi ngày, thần số học) là một hàng ô nhập ngay dưới tiêu đề; mỗi màn chỉ một khối
sage cho vật chính (lưới tháng, bàn 9 cung, lá số, bảng giá vàng, bảng giá Petrolimex, kết
quả xổ số dạng bảng giải x đài, khối "Bây giờ" của thời tiết, số chủ đạo); phần phụ nằm
trên nền trắng với tiêu đề 18px và vạch mảnh, các tầng cách nhau 40px. Lá số tử vi hiện bàn 4x4 khi khối rộng từ 680px; hẹp hơn thì
có sơ đồ 4x4 thu nhỏ, bấm để nhảy tới cung. Trên điện thoại thanh bên gập thành thanh
trên 56px + bảng danh mục toàn màn (không JS vẫn mở được bằng `#menu`).
Điều khiển dùng chung một bộ (web và `/admin`): nút dạng viên thuốc (38px trên web, 44px trên màn cảm ứng; 36/32px
trong admin), một nút chính xanh đen mỗi màn, nút phụ nền trắng viền 1px; chọn một trong vài
giá trị bằng segmented (âm/dương lịch, nam/nữ, khoảng 24 giờ/7 ngày/30 ngày); mọi bật/tắt là
công tắc `role="switch"` 32x20 (xanh đen khi bật); ô nhập + nút Lưu/Tra dính liền; nút bị khoá
vẫn đọc được. Chuyển động chỉ là phản hồi: bấm 120ms, rê chuột 150ms, công tắc/bảng 200ms.
CSS `static/site.css` là file riêng `/static/site.css?v=<hash nội dung>` cache 1 năm
(`immutable`): chỉ lượt xem đầu tiên tải thêm 1 request, các trang sau trình duyệt dùng
bản đã lưu, không hỏi lại server (HTML mỗi trang nhẹ đi ~50 KB; phông chữ tải từ Google). Nút đổi sáng/tối ở cuối thanh bên (lưu `localStorage` key
`lcta.theme`, không JS thì theo hệ máy); trang `/admin`, trang đăng nhập admin và
các trang Quyền riêng tư / Điều khoản / Xoá dữ liệu dùng cùng bảng màu và key này.
Mọi chữ đạt ≥ 4.5:1 trên nền của nó, viền ô nhập ≥ 3:1, ở cả sáng lẫn tối. Logo
LCTA có 2 bản `static/logo-light.webp` / `logo-dark.webp` (tự đổi theo nền) và
`static/favicon.png`, tạo bằng `scripts/make_site_logo.py` từ ảnh logo gốc nền đen.

| Đường dẫn | Nội dung |
|---|---|
| `/` | Trang chủ HTML cho trình duyệt VÀ trình đọc link Facebook/Zalo (gửi `Accept: */*`) để link chia sẻ có tiêu đề, mô tả, ảnh; chỉ health check (User-Agent `Render/…`, `UptimeRobot`…) và client gửi `Accept: application/json` nhận `{"status":"ok"}` (`web_site.wants_html`) |
| `/robots.txt`, `/sitemap.xml`, `/llms.txt` | `/llms.txt` (chuẩn llmstxt.org) tóm tắt site cho trợ lý AI/agent: link từng trang kèm tham số URL, danh sách JSON API (Google không dùng để xếp hạng). Robots chặn bot lấy dữ liệu huấn luyện AI (GPTBot, ClaudeBot, Google-Extended, Applebot-Extended, CCBot, Bytespider, meta-externalagent, cohere-ai: `Disallow: /`), vẫn cho bot tìm kiếm (Googlebot kể cả AI Overviews, Bingbot, OAI-SearchBot, Claude-SearchBot, PerplexityBot) và cho máy tìm kiếm đọc trang công khai, chặn `/admin`, `/api/`, `/internal/`, `/webhook`; sitemap liệt kê các trang công khai kèm trang tra cứu hay tìm (từng đài xổ số, thời tiết các nơi có sẵn, lịch 11 tháng tới), `lastmod` chỉ khi biết chắc (trang đổi số liệu mỗi ngày: hôm nay; đài xổ số: ngày quay gần nhất). Tiêu đề, mô tả riêng từng trang theo từ khoá hay tìm, trang giá có ngày (`Giá vàng hôm nay 27/10/2026: …`) và giá dòng đầu trong mô tả; JSON-LD (`WebSite` + `Organization` có logo `static/icon.png` ở trang chủ, `WebPage` + `BreadcrumbList` ở trang khác, `dateModified` theo giờ cập nhật giá); `theme-color`, `apple-touch-icon`; `GOOGLE_SITE_VERIFICATION` (mã cách "Thẻ HTML" của Google Search Console, dán phần content hoặc cả thẻ) in thẻ `google-site-verification` ở mọi trang công khai. Trang kết quả riêng (`/lich/hoi?q=`, lá số, thần số học, bàn Kỳ Môn theo giờ tự chọn, 404) và trang báo lỗi nhập liệu (URL gõ bừa: tháng/ngày/đài/địa điểm sai) có `noindex, follow`, không canonical; nguồn ngoài lỗi (giá vàng, xăng, xổ số, thời tiết) trả `503` + `Retry-After: 300` để máy tìm kiếm giữ bản cũ, người xem vẫn thấy thông báo. Mỗi ngày trong lịch là một trang riêng `/lich?ngay=YYYY-MM-DD` (tiêu đề, mô tả theo ngày âm, can chi, giờ tốt; link cũ kèm `?thang=` có canonical về đây); URL khác nhau cùng nội dung quy về một canonical (tháng hiện tại -> `/lich`, `10/2026` -> `?thang=2026-10`, TP.HCM/Sài Gòn -> `/thoi-tiet`, tên địa điểm gõ khác dấu -> tên trong danh sách, miền Nam -> `/xo-so`, tên đài/viết tắt/ngày gõ tay -> khoá đài + ngày ISO; sitemap không liệt kê bản trùng); H1 có từ khoá (`Lịch âm hôm nay`, `Lịch âm tháng 10/2026`, `Kết quả xổ số …`); tiêu đề trang tháng dùng từ hay tìm `Lịch vạn niên tháng 10/2026`. Sitemap thêm trang từng ngày lễ, ngày tốt 6 tháng tới theo từng việc, tuổi 1940 tới năm nay. Cuối mỗi trang gốc (không tham số) có mục **Hỏi đáp** 3 câu lấy từ `assets/site_faq.yaml` (sửa nội dung ở đây, link nội bộ viết `[chữ](/đường-dẫn)`); trang ngày/tháng/đài và trang `noindex` không lặp lại khối này. Không khai `FAQPage` vì Google đã bỏ kết quả nhiều định dạng của loại này. Mọi trang có `<link rel="canonical">`, thẻ `og:`/`twitter:` (ảnh `static/og-image.png` 1200x630, tạo bằng `scripts/make_og_image.py`); link tuyệt đối theo `SITE_BASE_URL` (trống = `WEBHOOK_BASE_URL`), canonical chỉ giữ tham số nội dung (bỏ `fbclid`, `utm_*`, `?tb=`, số vé). Đường dẫn không có: trình duyệt nhận trang 404 cùng giao diện (`noindex`), client khác và `/api` vẫn nhận JSON |
| `/lich?thang=YYYY-MM&ngay=YYYY-MM-DD` | Lịch tháng dương-âm + chi tiết ngày (link cũ `?q=`/`?am=` tự chuyển sang trang mới) |
| `/lich/hoi?q=...` | Hỏi lịch tự do (như lệnh `/lich`); câu không hiểu thì gợi ý cách hỏi kiểu web (`calendar_query.WEB_HELP`, không có cú pháp lệnh chat) |
| `/lich/doi-ngay?am=15/8/2027&nhuan=1` | Đổi ngày âm sang dương |
| `/lich/ngay-le` | Ngày lễ âm/dương trong 12 tháng tới (tên lễ dẫn tới trang riêng) |
| `/lich/le-hoi[?nam=2027&tinh=bac-ninh]` | 206 lễ hội, lễ tết Việt Nam theo năm, lọc theo 34 tỉnh: ngày dương + âm, nơi tổ chức, mô tả; bảng riêng cho 42 lễ ban tổ chức công bố ngày từng năm. Dữ liệu Nhật Nguyệt (CC BY 4.0, ghi nguồn cuối trang). Trang xem 1 ngày (`/lich?ngay=`) và API `/api/v1/calendar/days/` có thêm trường `festivals` |
| `/lich/ngay-le/<khoá>` (vd `tet-nguyen-dan`, `tet-trung-thu`) | Một ngày lễ: lần tới là ngày nào, thứ mấy, ngày âm, còn bao nhiêu ngày, 5 lần tới; giới thiệu ngắn + ngày nghỉ theo Điều 112 Bộ luật Lao động 2019 trong `assets/site_holidays.yaml`. Khoá = tên lễ trong `calendar_query.HOLIDAYS` bỏ dấu, bỏ phần trong ngoặc (`site_data.slug`); khoá lạ trả 404 |
| `/lich/ngay-tot?thang=YYYY-MM&viec=cuoi-hoi\|khai-truong\|dong-tho\|nhap-trach\|xuat-hanh\|ky-ket` | Ngày tốt trong tháng theo việc, cùng cách chọn với Hỏi lịch (`calendar_query.is_good_day`: hoàng đạo, tránh Tam nương/Nguyệt kỵ, trực hợp việc), kèm ngày âm, thần trực, trực, giờ hoàng đạo; tháng này canonical bỏ `?thang=`; chỉ tháng trước tới 24 tháng sau vào chỉ mục |
| `/tuoi`, `/tuoi/<năm sinh>` | Tuổi theo năm sinh (1900 tới năm nay; năm khác 404): can chi, con giáp, mệnh, khoảng ngày dương của năm âm, tuổi hợp/kỵ kèm năm sinh gần, bảng sao hạn/Tam tai/Kim lâu/Hoang ốc năm nay và năm sau (`can_chi.han_profile`, cùng số liệu với chat); `/tuoi?nam=1990` chuyển sang `/tuoi/1990`; trang mục lục liệt kê 1940 tới năm nay |
| `/thoi-tiet?noi=Đà Lạt` | Thời tiết hiện tại + dự báo 7 ngày (Open-Meteo, mặc định TP.HCM; tối đa 3 lượt tra song song, cache có trần trong `services/weather.py`). Open-Meteo lỗi thì dùng wttr.in: dòng nguồn của bảng dự báo ghi "wttr.in (dự phòng, tối đa 3 ngày)" và tiêu đề ghi số ngày thật (3), không thêm băng báo riêng, chủ bot nhận cảnh báo nhẹ (tối đa 1 lần/6 giờ); mô tả wttr.in dịch theo mã thời tiết, không lẫn tiếng Anh; tên địa điểm sửa "Ð" -> "Đ" và tên tỉnh có dấu. Tra địa điểm MỚI (không trong danh sách hay xem, chưa có trong cache toạ độ - kể cả kết quả "không tìm thấy") giới hạn 10 lượt/10 phút mỗi IP và 60 lượt/10 phút toàn site; tra lại địa điểm đã cache không trừ lượt |
| `POST /thoi-tiet/dia-diem/them`, `/xoa` (cần đăng nhập) | Thêm/xoá địa điểm hay xem |
| `/xo-so?dai=mn\|mt\|mb\|<khoá đài>&ngay=YYYY-MM-DD&ve=` | Kết quả xổ số theo miền/đài (ô chọn đánh dấu ★ đài quay hôm nay), bảng giải, dò vé bằng code; tối đa 3 lượt tải nguồn song song, không cần đăng nhập |
| `/gia-vang` | Giá vàng mua/bán (triệu đồng/lượng, luôn 2 số lẻ cho thẳng cột: 138,70 / 135,00; đơn vị ghi ngay trên bảng) kèm ▲▼ so với hôm trước: SJC miếng, nhẫn SJC, DOJI Hà Nội/TP.HCM/nữ trang, PNJ, Bảo Tín, Viettin, VN Gold; vàng thế giới XAU/USD + quy đổi tham khảo theo giá bán USD Vietcombank (chưa gồm thuế phí) và chênh lệch với SJC miếng; mục tỷ giá Vietcombank các ngoại tệ chính; giờ cập nhật + nguồn (vang.today, vang.today lỗi thì bảng SJC). Dùng cache sẵn có của `services/vn_market_data.py` (10 phút) qua `services/price_lookup.py` |
| `/gia-xang` | Giá xăng dầu Petrolimex (vùng 1, vùng 2, tăng giảm kỳ này/kỳ trước) và PVOIL, giờ cập nhật, nguồn giaxanghomnay.com (`services/fuel_prices.py`, cache 1 giờ). Hai trang giá: tối đa 3 lượt tải nguồn song song, hạn chót 15 giây, nguồn lỗi thì hiện thông báo thay vì trang lỗi; bảng gập thành thẻ khi khối hẹp (điện thoại 360px không cuộn ngang) |
| `/tu-vi` (form, POST ra trang kết quả) | Lá số Tử Vi Đẩu Số dạng lưới 4x4, sao kèm độ sáng (M/V/Đ/B/H) và Tứ hóa; dưới lá số là mục "Diễn giải lá số" cho mọi người xem (không AI, `tuvi.reading`): quan hệ cục - mệnh, Thân cư, đại hạn đang đi theo tuổi âm, cách đọc lá số và từng cung (lo việc gì, chính tinh sáng hay hãm, Tứ hóa, cát/sát tinh, Tuần Triệt, đọc nhanh thuận / trung bình / nhiều trắc trở). Lời văn trong `assets/tu_vi.yaml`, sửa không cần đổi code |
| `/than-so-hoc` (form, POST ra trang kết quả) | Thần số học: số chủ đạo, biểu đồ ngày sinh / tổng hợp, 4 đỉnh cao, 4 thử thách |
| `/ky-mon?ngay=YYYY-MM-DD&gio=HH:MM` (mặc định lúc này; `POST /ky-mon` = AI luận giải, cần đăng nhập) | Bàn Kỳ Môn 9 cung dạng lưới 3x3 (giữ 3x3 ở mọi bề rộng), bảng thông số, cách cục theo nhóm, ý nghĩa từng cung; mục "Diễn giải bàn" (không AI, `kymon.reading`): Trực phù, Trực sử, không vong, Mã tinh bằng lời thường, hướng nên dùng / nên tránh (chấm theo cát hung của cửa, sao, thần, không vong, cách cục) và "việc gì xem hướng nào" theo `viec_xem` trong `assets/ky_mon.yaml`; có link từ thanh bên, trang chủ và menu "Công cụ web" của `/admin` |
| `GET /api/v1/calendar/days/{YYYY-MM-DD}`, `/months/{YYYY-MM}`, `/conversions?lunarDay=&lunarMonth=&lunarYear=&isLeapMonth=`, `/answers?q=` | JSON lịch |
| `GET /api/v1/lottery?province=&date=&ticket=` | JSON xổ số: `province` = `mn`/`mt`/`mb` hoặc khoá/tên đài (`tay-ninh`, `Đà Lạt`; trống = miền Nam), `date` YYYY-MM-DD (trống = kỳ mới nhất), `ticket` tuỳ chọn; trả `target` (lịch quay `drawDays`, `drawsToday`), `status` (`latest`/`exact`/`not_drawn`/`pending`/`missing`/`empty`), `message`, `draw.provinces[].prizes`, `ticket.results[].wins`, `availableDates`; 422 `INVALID_PROVINCE`/`INVALID_DATE`/`INVALID_TICKET`, nguồn lỗi 503 `SOURCE_UNAVAILABLE` |
| `GET /api/v1/gold` | JSON giá vàng: `source` (`vang.today` hoặc `sjc.com.vn`), `fallback` (true = vang.today lỗi, đang dùng bảng SJC, không có tăng giảm/thế giới), `updated`, `unit` (`VND/lượng`), `domestic[]` {code, name, brand, branch, buy, sell, changeBuy, changeSell} (đồng/lượng, tăng giảm so với hôm trước, `null` khi không có), `world` {priceUsdPerOunce, change, unit, `conversion` {vndPerTael, usdVnd, rateSource, rateUpdated, sjcSellGap, note} hoặc null khi thiếu tỷ giá} hoặc null, `fx` (như `/fx`, null khi tỷ giá lỗi); cả vang.today lẫn SJC lỗi → 503 `SOURCE_UNAVAILABLE` |
| `GET /api/v1/fuel` | JSON giá xăng dầu: `source`, `updated`, `unit` (`VND/lít`), `petrolimex[]` {name, kind (`xang`/`diesel`/`dau_hoa`/`khac`), zone1, zone2, change, prevChange}, `pvoil[]` {name, kind, price, change, prevChange}; nguồn lỗi 503 `SOURCE_UNAVAILABLE` |
| `GET /api/v1/fx` | JSON tỷ giá Vietcombank: `source`, `updated`, `unit` (`VND`), `rates[]` {code, name, nameVi, cashBuy, transferBuy, sell} cho USD, EUR, JPY, CNY, KRW, GBP, AUD, SGD, THB (mã nào nguồn thiếu thì bỏ); nguồn lỗi 503 `SOURCE_UNAVAILABLE` |
| `POST /api/v1/numerology/readings`, `POST /api/v1/tuvi/charts` | JSON thần số học/lá số (ngày sinh trong body, không nằm trong URL/log; `birthDate` nhận `YYYY-MM-DD` hoặc `DD/MM/YYYY`). Lá số: `isLeapMonth`/`isEarlyTy` là bool (sinh 23h-24h: `isEarlyTy: true`, an theo ngày hôm sau); mỗi cung có `mainStars`/`minorStars` (tên) như cũ và thêm `stars[]` {name, isMain, hoa, brightness M/V/Đ/B/H}, `daiHan.fromAge/toAge`, `viewYear` (body, tuỳ chọn 1900-2100; sai kiểu 422 `INVALID_VIEW_YEAR`), `vanNam` {year, canChi, age, luuNien, tieuHan, daiHan, luuTuHoa[], saoLuu[]} và `notes[]` (lưu ý quy ước của lá số); thần số học thêm `asOf`, `age`, `lifePath.single`, `maturity`, `pinnacles[]`, `challenges[]`, `personalMonthDetail`, `personalDay` {date, value, meaning}, `karmicDebt` ({value, meaning} hoặc null), `nameChart`, `combinedChart` |
| `POST /api/v1/qimen/charts` | JSON bàn Kỳ Môn: body `{"dateTime"?: "YYYY-MM-DDTHH:MM[+07:00]"}` (không múi giờ = giờ VN, bỏ trống = lúc này); trả `pillars`, `solarTerm`, `dun`, `ju`, `yuan`, `xunShou`, `zhiFu`, `zhiShi`, `void`, `horse`, cờ phục/phản ngâm, `grid`, `palaces`, `patterns`, `patternGroups`; GET trả 405 chuẩn |

Lỗi nhập liệu là HTTP 422 `{"error": {"code": "...", "message": "..."}}` (body JSON
của các API POST tối đa 4 KB); 404/405 dưới `/api/v1` cũng cùng dạng `{"error"}`
(body quá 1 MiB bị middleware chặn 413).
Không mở `/docs`, `/openapi.json`. Khoá JSON camelCase; chỉ thêm trường mới, không
đổi/bỏ trường cũ (`/days` trả `weekday` là tên + `weekdayIndex` số; `/months` trả
`weekday` số + `weekdayName`).

### Tốc độ và bộ nhớ đệm

- Nén gzip mọi phản hồi từ 1 KB (trừ `/internal/*` - gateway Zalo gọi qua loopback, và
  luồng `text/event-stream` của avatar): `site.css` 64 KB còn 16 KB, `/lich/le-hoi` 120 KB
  còn 26 KB.
- Trang HTML và API lịch (`/api/v1/calendar/days|months`) có ETag yếu: xem lại trang
  không đổi trả 304, thân rỗng. Trang chỉ phụ thuộc URL (tháng/ngày khác hôm nay, tuổi,
  lễ hội theo năm...) gửi `Cache-Control: public, max-age=3600` cho người chưa đăng
  nhập (không vượt quá nửa đêm giờ VN); trang "hôm nay" vẫn `no-cache`; có `Vary: Cookie`.
- `/lich/le-hoi` giữ tối đa 32 bản đã nén sẵn theo (năm, tỉnh, đăng nhập): xem lại ~1 ms.
- Phông Google Fonts nạp không chặn hiển thị (preload + đổi `media` bằng script, có
  `<noscript>`). Tự host phông (woff2) là việc còn lại - cần tải file qua mạng.
- Ba trang `/privacy`, `/terms`, `/data-deletion` đọc 1 lần, `max-age=86400`.
- `Permissions-Policy` tắt camera, micro, định vị, thanh toán, USB; chỉ `/admin/avatar`
  được dùng micro và `/thoi-tiet` được hỏi vị trí.
- Nút "AI luận giải" chờ tối đa 20 giây khi bot đang trả lời việc khác rồi báo bận.

## App Android màn hình thông tin (`android/`)

Điện thoại cũ (nhắm tới Samsung J2 Prime, Android 6) thành màn hình để bàn: giờ, lịch
âm dương, thời tiết, watchlist chứng khoán, vàng, xăng, tỷ giá, tin RSS; cập nhật 5 phút
khi màn hình sáng, giờ đêm tự tắt và đánh thức bằng cảm biến tiệm cận. Cài đặt, ghép
máy, ký APK: [android/README.md](../android/README.md).

- Ghép (chỉ chủ bot, Telegram): `/app moi <tên>` → mã 8 ký tự hết hạn 10 phút, dùng 1
  lần; `/app` liệt kê, `/app xoa <số>` thu hồi (tối đa 5 thiết bị). DB (bảng
  `app_devices`, migration 030) chỉ lưu SHA-256 của mã ghép và token. Ghép sai quá 20
  lần/10 phút thì khoá mọi lượt ghép tới hết cửa sổ (`services/app_devices.py`).
  `/app` không có trong menu lệnh Telegram (menu đã đủ 100 lệnh) nhưng vẫn gõ được.
- `POST /api/v1/app/pair` `{"code": "K7QF-3MXA"}` → `{"token": "la_...", "device"}`.
- `GET /api/v1/app/dashboard[?place=Hà Nội]` với `Authorization: Bearer la_...` → MỘT
  JSON: `calendar` (`today` + `month`), `weather`, `gold`, `fuel` (Petrolimex), `news`,
  `watchlist` (danh sách `/theodoi` của người sở hữu thiết bị), `refreshSec` (300).
  Phần nào lỗi trả `{"error": ...}`, phần khác vẫn có; gzip; `Cache-Control: no-store`.
  401 khi token sai/đã thu hồi, 503 khi DB sập mà thiết bị chưa xác thực trong 6 giờ qua.
- Tải máy chủ (`services/app_dashboard.py`): không gọi AI, không chiếm khoá lượt chat;
  cả bản trả về cache 60 giây cho mỗi (người, địa điểm), tối đa 8 bản; tin RSS cache 15
  phút; giá cổ phiếu cache 2 phút trong phiên, 30 phút ngoài phiên (tối đa 64 mã,
  `APP_WATCHLIST_MAX` mã mỗi lần, mặc định 20); vàng/tỷ giá/xăng/thời tiết dùng cache sẵn
  có. Xác thực token cache 5 phút trong RAM (thu hồi xoá cache ngay).
- APK build bằng GitHub Actions (`.github/workflows/android.yml`): chạy ở PR/nhánh khi
  `android/` đổi hoặc bấm tay; KHÔNG chạy trên push main để không chặn Render
  `checksPass`.

## Trang quản trị (Admin dashboard)

`web.py` phục vụ 1 trang HTML tại `/admin` (đăng nhập bằng `ADMIN_USER`/`ADMIN_PASS`,
phiên đăng nhập ký bằng HMAC với khoá dẫn xuất HKDF từ `ADMIN_PASS` + `SETTINGS_ENC_KEY`
kèm 1 "epoch" lưu trong bảng settings (`admin_session_epoch`) — đổi `ADMIN_PASS`,
`SETTINGS_ENC_KEY` hoặc bấm **Đăng xuất** sẽ vô hiệu MỌI phiên đang mở; cookie lộ ra
cũng không dùng để dò ngược mật khẩu được). Không đặt các biến này thì `/admin` luôn từ
chối truy cập; DB không đọc được thì đăng nhập trả 503 (fail closed).

Chống CSRF: mọi request đổi dữ liệu của `/admin/api/*` phải là `Content-Type:
application/json` (415 nếu không), và request (kể cả đăng nhập/đăng xuất) mang
`Sec-Fetch-Site: cross-site`/`same-site` do trang khác gây ra bị từ chối. API quản trị
trả `Cache-Control: no-store`.

Chống dò mật khẩu: khoá theo IP sau 5 lần sai/15 phút (IP lấy từ phần tử
`X-Forwarded-For` do proxy Render nối vào, xem `ADMIN_TRUSTED_PROXY_HOPS`); khi
có nhiều lần sai từ nhiều IP, mọi lượt đăng nhập bị làm chậm thêm tối đa 3 giây
thay vì khoá toàn cục — mật khẩu đúng vẫn luôn vào được. Log lượt sai có kèm chuỗi
`X-Forwarded-For` để kiểm tra trên production rằng IP được chọn đúng. Mọi dữ liệu
hiển thị trên trang đều được escape HTML. Trang cho phép:

- Bật/tắt 9Router, Tavily, Agnes AI (tạo ảnh) và trí nhớ dài hạn theo từng user.
- Reset cooldown provider, đổi `PROVIDER_ORDER`.
- Đổi model và **API key** override theo từng provider (bao gồm 9Router); riêng
  9Router còn có ô đổi **Base URL** (để trống = dùng `ROUTER9_BASE_URL` từ env).
- Xem thống kê **lượt gọi theo user/kênh** và **theo model** (`/admin/api/usage`,
  `/admin/api/usage/models`, biểu đồ theo ngày/giờ `/admin/api/usage/daily`) — cùng
  số liệu với lệnh `/thongke` (docs/tro-ly.md), chỉ khác là xem trên web thay vì chat.
- Thống kê AI: bảng kết quả và độ trễ p50/p95 theo provider
  (`/admin/api/usage/outcomes`), quota miễn phí theo cửa sổ (`/admin/api/usage/burn`),
  "Lượt trả lời gần đây" với thời gian chờ khoá, chờ slot, gom ngữ cảnh, LLM, gửi
  tin, provider cuối, có đổi provider không (`/admin/api/usage/turns`); bấm mã lượt
  để xem từng lượt gọi provider (`/admin/api/usage/turn?turn_id=`).
- Người dùng & Zalo: xem, ghim, quên từng mục trí nhớ của từng người
  (`/admin/api/memory/items`, `/admin/api/memory/item`); sửa trần lượt/lượt nặng mỗi
  ngày của thành viên Zalo kèm số đã dùng hôm nay (`/admin/api/quota`, áp dụng ngay).
- Hệ thống → "Việc nền & tác vụ định kỳ" (`/admin/api/jobs`): 30 việc gần nhất (đang
  chạy/chờ trước, thời gian chạy, token, lỗi) và danh sách tác vụ định kỳ, số lần
  chạy hôm nay so với trần.
- Nhật ký (`/admin/api/journal?user_id=&q=&page=`, `POST /admin/api/journal/note`):
  ghi chú phiên chat của chủ bot hoặc 1 người Zalo gom theo ngày (giờ VN) kèm tóm tắt
  dài hạn, tìm theo từ khoá, sửa/xoá từng ghi chú (trong khoá trí nhớ của người đó;
  POST chỉ nhận JSON), 20 ghi chú/trang, tối đa 50 trang.
- Hệ thống → "Giám sát, sao lưu & lỗi" (`/admin/api/watchdog`): lỗi hôm nay theo
  loại, số lần khởi động lại trong 7 ngày, nhịp tim, lần sao lưu và diễn tập gần
  nhất, trạng thái xoay khoá (nút "Xoay khoá ngay" = `POST /admin/api/key-rotation/run`),
  dòng "Gateway Zalo"; thẻ RAM có thêm working set và `memory.events`. KPI RAM hiện
  working set (cùng số watchdog dùng, không tính page cache), số cgroup gồm cả cache
  ở dòng phụ; có dòng "Dọn heap (malloc_trim)" (số lần, RSS trước/sau lần gần nhất).

Giao diện: dashboard responsive (sidebar/topbar, 360px không cuộn ngang), sáng/tối
tự động + nút đổi, KPI tile (provider, lượt gọi, RAM, DB, Zalo), biểu đồ SVG
inline không thư viện ngoài, bảng gập thành thẻ trên điện thoại, tự làm mới theo
khoảng chọn (mặc định 60 giây). Không script/CDN ngoài; mọi giá trị server vẫn escape.

Trang tải mọi khối số liệu sống bằng **1 request** `GET /admin/api/overview` (trước đây
12 request song song mỗi 30 giây): server giữ cache có trần (thống kê 60 giây; dung lượng
DB, giám sát, việc nền 5 phút - phép đo dung lượng DB quét cả bảng nên không chạy mỗi
lần mở trang), khối nào lỗi chỉ hiện trong `errors`. Nút **Làm mới** gửi `?fresh=1` để đo
ngay. Các endpoint cũ vẫn dùng được.

## Meta App: Privacy Policy, Terms và Data Deletion

Repo cung cấp sẵn ba trang public, **không cần đăng nhập `/admin`**:

- `https://<domain-render>/privacy` — Privacy Policy / Chính sách quyền riêng tư.
- `https://<domain-render>/terms` — Terms of Service / Điều khoản dịch vụ.
- `https://<domain-render>/data-deletion` — User Data Deletion Instructions / Hướng dẫn xóa dữ liệu.

Sau khi deploy lên Render, mở cả ba URL trên trình duyệt để chắc chắn chúng trả HTTP 200. Trong **Meta for Developers → Cài đặt ứng dụng → Thông tin cơ bản**, điền:

```text
Miền ứng dụng:             <domain-render>             (không có https://)
URL chính sách riêng tư:   https://<domain-render>/privacy
URL Điều khoản dịch vụ:    https://<domain-render>/terms
Xóa dữ liệu người dùng:    URL hướng dẫn xóa dữ liệu
URL xóa dữ liệu:           https://<domain-render>/data-deletion
```

Ví dụ nếu service Render là `https://my-bot.onrender.com` thì miền ứng dụng là `my-bot.onrender.com`.

Các trang này không hiển thị access token, cookie, App Secret hoặc thông tin đăng nhập. Phần liên hệ hướng người dùng tới thông tin liên hệ mà chủ ứng dụng công bố trên Facebook Page/Meta App, vì vậy hãy bảo đảm **Email liên hệ** trong Meta App Settings là email bạn đang sử dụng.
