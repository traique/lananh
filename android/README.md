# Lan Anh Màn hình - app Android

Biến một điện thoại cũ (đã nhắm tới Samsung Galaxy J2 Prime, Android 6) thành màn hình
thông tin để bàn hoặc treo tường:

- **Trái (cố định):** giờ, thứ ngày dương lịch, ngày âm lịch + can chi, ngày lễ/tiết
  khí, thời tiết hiện tại và 5 mốc 2 giờ tới.
- **Phải (tự đổi trang, chạm để sang trang):** lịch tháng âm dương · watchlist chứng
  khoán · vàng, xăng, tỷ giá · tin RSS · dự báo 4 ngày.
- Dữ liệu lấy từ bot (`GET /api/v1/app/dashboard`) **mỗi 5 phút khi màn hình đang
  sáng**; mất mạng vẫn hiện bản gần nhất (ghi "dữ liệu cũ").
- **Giờ ngày luôn sáng; giờ đêm** giảm độ sáng, không ai chạm/vẫy tay vài phút thì màn
  hình tự tắt; **đưa tay lại gần cảm biến tiệm cận** (cạnh loa thoại) để bật lại.

App không dùng thư viện ngoài (chỉ Kotlin + API có sẵn của Android) để nhẹ trên máy
RAM 1,5 GB và chạy được từ Android 6 (minSdk 23).

## 1. Lấy file APK

1. Trên GitHub: **Actions → Android APK → Run workflow** (chọn nhánh `main`), chờ
   khoảng 5 phút.
2. Mở lượt chạy vừa xong → mục **Artifacts** → tải `lananh-manhinh-apk` (file zip, bên
   trong là `app-release.apk`).
3. Chép APK vào điện thoại. Trên J2 Prime: **Cài đặt → Màn hình khoá và bảo mật →
   Nguồn không xác định** (bật) → mở file APK để cài.

### Ký APK cố định (nên làm 1 lần)

Không có keystore riêng thì mỗi lần build APK được ký bằng một debug key khác nhau →
cài bản mới phải **gỡ bản cũ trước** (mất cài đặt, phải ghép lại). Tạo keystore 1 lần
(máy có Java):

```bash
keytool -genkeypair -v -keystore lananh.jks -alias lananh -keyalg RSA -keysize 2048 -validity 10000
base64 -w0 lananh.jks > lananh.jks.b64   # macOS: base64 -i lananh.jks -o lananh.jks.b64
```

Rồi vào **GitHub → Settings → Secrets and variables → Actions** thêm 4 secret:
`ANDROID_KEYSTORE_BASE64` (nội dung file .b64), `ANDROID_KEYSTORE_PASSWORD`,
`ANDROID_KEY_ALIAS` (`lananh`), `ANDROID_KEY_PASSWORD`. Giữ file `lananh.jks` cẩn thận
(mất là phải gỡ app cài lại); KHÔNG commit vào repo.

## 2. Ghép với bot

1. Trên Telegram nhắn bot: `/app moi Phòng khách` → bot trả **mã ghép 8 ký tự**
   (vd `K7QF-3MXA`), hết hạn sau 10 phút, dùng 1 lần.
2. Mở app (lần đầu tự vào Cài đặt; về sau **giữ lâu vào đồng hồ**) → nhập **địa chỉ bot**
   (vd `lananh.onrender.com`) và mã ghép → **Ghép**.
3. `/app` xem các thiết bị đã ghép (tối đa 5), `/app xoa <số>` thu hồi - app trên máy đó
   báo "đã bị thu hồi" ở lần cập nhật kế tiếp.

Watchlist là danh sách `/theodoi` của chủ bot; thời tiết theo thành phố mặc định
(`/thoitiet nha <tỉnh>`) hoặc địa điểm nhập trong Cài đặt của app.

## 3. Cài đặt điện thoại (J2 Prime / Android 6)

| Việc | Ở đâu | Vì sao |
|---|---|---|
| Thời gian chờ màn hình **15 hoặc 30 giây** | Cài đặt → Màn hình | Giờ đêm app chỉ "thôi giữ sáng"; hệ thống tắt màn hình theo thời gian chờ này |
| Khoá màn hình **Không** (hoặc Vuốt) | Cài đặt → Màn hình khoá và bảo mật | Màn hình bật lên là thấy ngay thông tin, không phải mở khoá |
| **Cho app chạy nền** | App → Cài đặt → nút "Cho app chạy nền" | Dịch vụ đọc cảm biến không bị hệ thống tắt |
| Smart Manager → Pin → **Ứng dụng chưa giám sát** → thêm app | Samsung Smart Manager | Samsung tự "ngủ" app chạy nền nếu không thêm vào đây |
| Cắm sạc liên tục, để chỗ thoáng | | Xem mục pin bên dưới |

Treo máy nằm ngang: cảm biến tiệm cận ở cạnh có loa thoại; đưa tay cách khoảng 3-5 cm
ngay chỗ đó. Lần đầu nên thử: tắt màn hình (để hết thời gian chờ) rồi vẫy tay.

**Nếu vẫy tay không bật được màn hình**: một số máy tắt hẳn cảm biến khi tắt màn hình
dù app đã giữ CPU thức. Khi đó chọn "Luôn sáng 24/24" (kèm độ sáng đêm thấp) hoặc dùng
nút nguồn.

**Pin:** máy cũ cắm sạc 24/7 có thể phồng pin. Pin J2 Prime tháo được - thỉnh thoảng
kiểm tra; thấy nắp lưng cộm/pin phồng thì thay pin. Có thể cắm qua ổ hẹn giờ để sạc
ngắt quãng.

## 4. Mạng, pin, tải máy chủ

- App chỉ gọi API khi màn hình đang sáng: 1 request (~5-20 KB, nén gzip) mỗi 5 phút,
  lỗi thì thử lại sau 1 phút. Màn hình tắt = không gọi mạng.
- Máy chủ cache cả bản trả về 60 giây và từng nguồn (vàng/tỷ giá 10 phút, xăng 1 giờ,
  thời tiết 10 phút, tin 15 phút, giá cổ phiếu 2 phút trong phiên / 30 phút ngoài phiên),
  không gọi AI - xem `services/app_dashboard.py`.
- Android 6 thiếu chứng chỉ gốc Let's Encrypt (ISRG) và Google Trust Services; app mang
  sẵn các gốc đó trong `app/src/main/res/raw/` và dùng CỘNG với kho của máy.

## 5. Build trên máy

Cần JDK 17 và Android SDK (Android Studio có sẵn cả hai). Mở thư mục `android/` bằng
Android Studio, hoặc dùng Gradle 8.11 trở lên:

```bash
cd android
gradle assembleRelease   # APK: app/build/outputs/apk/release/app-release.apk
```

Repo không kèm Gradle wrapper (file .jar); CI dùng `gradle/actions/setup-gradle` với
Gradle 8.11.1.

## Cấu trúc mã

| File | Việc |
|---|---|
| `MainActivity.kt` | Bố cục, đồng hồ, đổi trang, lấy dữ liệu 5 phút, giữ sáng/tắt màn hình |
| `MonthView.kt` | Lưới lịch tháng dương + âm vẽ bằng Canvas |
| `WakeService.kt` | Dịch vụ nền đọc cảm biến tiệm cận, bật màn hình |
| `SettingsActivity.kt` | Ghép thiết bị, giờ ngày/đêm, độ sáng, cảm biến |
| `Api.kt` | Gọi API (HttpURLConnection + chứng chỉ gốc mang theo), lưu bản gần nhất |
| `Prefs.kt`, `Ui.kt`, `BootReceiver.kt` | Cài đặt, màu/định dạng số, tự mở sau khởi động |
