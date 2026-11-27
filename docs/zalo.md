# Zalo, Zoom và Zalo → Facebook Page

Kênh Zalo (nhiều tài khoản, phân quyền, tổng kết nhóm, tín hiệu cổ phiếu), luồng đăng bài từ nhóm Zalo lên Facebook Page, Page chứng khoán và Zoom Team Chat.

Tài liệu chính: [README.md](../README.md).

## Zalo

- Gateway `zca-js` tùy chọn chạy cạnh Python service.
- Đăng nhập tài khoản bot bằng QR từ Telegram.
- **Nhiều tài khoản Zalo** có thể nhắn cho bot, mỗi tài khoản có role riêng
  (`admin` hoặc `user`) và có thể khóa/mở khóa/xóa độc lập — xem mục
  "Zalo — nhiều tài khoản & phân quyền" bên dưới.
- Người ghép đôi ĐẦU TIÊN (qua mã `/pair`) tự động là admin đầu tiên; owner
  Telegram cấp quyền thêm cho người khác qua `/zalopair`, `/zaloadmin` (kèm cách
  xưng hô và lời chào lần đầu).
- Chỉ **admin** dùng được lệnh quản lý/xem lại nhóm (`/nhom`, `/themnhom`,
  `/xoanhom`, `/tongket`, `/dangnoi`); **thành viên** (`role=user`) chỉ chat và
  dùng các lệnh cá nhân bình thường (`/prompt`, `/gia`, `/dich`, `/reset`...),
  nhận bản cổ phiếu tham khảo và có giới hạn lượt mỗi ngày.
- Thu thập text từ nhóm allowlist, tạo summary và gửi qua durable outbox.
  Mỗi tin outbox được gửi/thử lại độc lập (backoff 2 → 30 phút, tối đa
  `ZALO_OUTBOX_MAX_ATTEMPTS` = 5 lần rồi bỏ cuộc và báo chủ bot), nên 1 người nhận
  chặn bot không làm kẹt tin của người khác; tin dài gửi dở được gửi tiếp từ đoạn kế.
- Tin riêng xử lý tuần tự theo TỪNG người gửi, song song giữa người khác nhau
  (`ZALO_DIRECT_CONCURRENCY`, mặc định 1 = số chỗ thành viên
  `MAX_CONCURRENT_TURNS - OWNER_RESERVED_SLOTS`; mỗi người chờ tối đa
  `ZALO_DIRECT_SENDER_BACKLOG` = 8 tin).
- Tin riêng phân loại theo `msgType` (`zalo-gateway/src/msgtype.ts`): ảnh, ảnh vẽ
  tay và ảnh gửi dạng file đi đường viết prompt/hỏi đáp ảnh (bot báo "em nhận được
  ảnh rồi, đợi em xem xíu nha" NGAY khi nhận); tin thoại được chép lời
  (`POST /internal/zalo/voice`); file PDF/DOCX/TXT/MD được đọc
  (`POST /internal/zalo/file`); video, vị trí, file khác nhận 1 câu "chưa hỗ trợ"
  (tối đa 1 lần/phút mỗi loại); sticker và ảnh động: im lặng. Câu báo do Python
  dựng theo cách gọi riêng (`POST /internal/zalo/notice`); người lạ không nhận gì,
  thành viên đã hết lượt không nhận câu "đã nhận ảnh".
- Gộp tin: các dòng chữ gõ liền nhau ("em ơi" / "mai trời mưa ko" / "ở Đà Lạt")
  thành 1 lượt. Gateway chờ 2 giây sau dòng cuối, tối đa 4 dòng hoặc 6 giây
  (`ZALO_DIRECT_DEBOUNCE_MS`, `..._MAX_MS`, `..._MAX_MESSAGES`); các dòng gõ trong lúc
  bot đang trả lời câu trước được gộp thành 1 lượt kế tiếp. Lệnh, link, ảnh, ghi âm,
  file đẩy phần đang chờ đi ngay để giữ thứ tự.
- Lượt dài có phản hồi: "đang soạn tin" (làm mới mỗi 6 giây, tối đa 30 lần), "đã
  xem", 👌 vào tin phải chờ lượt trước - chỉ với người bot đã từng trả lời (sau khi
  Render khởi động lại, lượt đầu của thành viên chưa có), tắt bằng
  `ZALO_PRESENCE_EVENTS=0`.
- `/huy` và lệnh chỉ đọc chạy "làn nhanh" ngoài hàng đợi theo người gửi (có trần),
  dùng được khi bot đang trả lời dở một câu dài; `/huy` còn bỏ các dòng chữ vừa gõ
  mà chưa gửi đi.
- Định dạng: câu trả lời gửi kèm `styles` zca-js (đậm, nghiêng, tiêu đề; tin cổ
  phiếu/danh mục tô xanh % tăng, đỏ % giảm); Zalo từ chối định dạng thì tự gửi lại
  đúng 1 lần bản chữ thuần.
- Nhịp gửi: nghỉ ngẫu nhiên 0,6-1,5 giây giữa các đoạn và giữa các tin outbox
  (`ZALO_SEND_GAP_MIN_MS`/`ZALO_SEND_GAP_MAX_MS`); outbox (nhắc việc, tóm tắt nhóm,
  xem trước bài Facebook, bản tin) tối đa `ZALO_OUTBOX_MAX_PER_HOUR` = 60 tin/giờ,
  vượt thì hoãn sang lượt sau, không mất tin. Gửi tuần tự theo người nhận, câu
  trả lời làn nhanh không chen giữa các đoạn.
- `/admin` → Hệ thống → "Giám sát" có dòng "Gateway Zalo": trạng thái kết nối và bộ
  đếm từ lúc gateway khởi động (tin riêng, tin gộp, làn nhanh, tin thoại, file, tin
  đã gửi, số lần rơi về chữ thuần, outbox gửi/lỗi/hoãn, hàng đợi).
- Endpoint nội bộ `/internal/zalo/*` chỉ nhận request từ loopback
  (`ZALO_BRIDGE_ALLOWED_HOSTS`, mặc định `127.0.0.1,::1`) kèm `ZALO_BRIDGE_SECRET`;
  request đi qua proxy (có `X-Forwarded-For`) bị từ chối.
- Tổng kết hằng ngày theo `Asia/Ho_Chi_Minh`; bật/tắt từng nhóm bằng
  `/tongket on|off [nhóm]` (mặc định bật, tắt vẫn lưu tin để `/tongket` thủ công).
- Chi tiết gateway: `zalo-gateway/README.md`.

## Thiết lập Zalo

Đặt `ZALO_ENABLED=true`, cấu hình `ZALO_BRIDGE_SECRET` và redeploy. Trong Telegram gửi:

```text
/zalo
```

Quét QR bằng tài khoản Zalo bot và xác nhận trên điện thoại. Gửi `/zalo` lần nữa để nhận mã pairing, sau đó từ tài khoản Zalo của bạn nhắn cho bot:

```text
/pair 123456
```

Tài khoản này tự động trở thành **admin đầu tiên** (đầy đủ mọi tính năng, kể cả lệnh nhóm).

Dùng `/zalologout` trên Telegram để xóa session Zalo đã lưu.

### Zalo — nhiều tài khoản & phân quyền

Nhiều người có thể cùng nhắn cho bot Zalo, mỗi người có 1 trong 2 quyền:

- **admin** (👑): dùng được MỌI tính năng, kể cả lệnh quản lý/xem lại nhóm
  (`/nhom`, `/themnhom`, `/xoanhom`, `/tongket`, `/dangnoi`).
- **user** (👤, thành viên): chỉ dùng được tính năng cá nhân bình thường
  (chat, `/prompt`, `/gia`, `/dich`, `/reset`, `/history`...) — KHÔNG thấy và
  KHÔNG dùng được lệnh nhóm, `/fb_*` hay lệnh quản trị (`/help` của thành viên
  không có các mục đó), dù có gõ đúng cú pháp cũng bị bỏ qua như tin nhắn không
  hỗ trợ. Phân tích cổ phiếu chỉ là bản tham khảo (không dòng hành động/kế hoạch)
  và có giới hạn lượt mỗi ngày (bên dưới).

Khi 1 tài khoản Zalo CHƯA được cấp quyền nhắn cho bot, bot **im lặng** với
người đó (không lộ ra là bot chưa cấu hình xong, gateway không gửi "đã xem"/"đang
soạn tin") và chỉ âm thầm báo owner qua Telegram kèm id_zalo cùng 2 nút [👤 Cấp
thành viên] [👑 Cấp admin]. Owner dùng các lệnh sau (chỉ trên Telegram) để quản lý:

| Lệnh | Chức năng |
|---|---|
| `/zalopair <id_zalo> [tên] [anh\|chị\|cô\|chú\|bạn\|em]` | Cấp quyền THÀNH VIÊN; từ viết thường cuối cùng là cách Lan Anh gọi người đó; lần pair đầu gửi lời chào qua Zalo |
| `/zaloadmin <id_zalo> [tên] [xưng hô]` | Cấp/nâng quyền ADMIN (dùng được lệnh nhóm) — hỗ trợ nhiều admin; nâng từ thành viên chỉ báo 1 câu ngắn |
| `/zalohaquyen <id_zalo>` | Hạ 1 admin về thành viên thường |
| `/zalokhoa <id_zalo>` | Khóa 1 tài khoản (không chat được nữa) |
| `/zalomokhoa <id_zalo>` | Mở khóa |
| `/zaloxoa <id_zalo>` | Xóa pairing kèm toàn bộ dữ liệu cá nhân (trí nhớ, lịch sử, ghi chú, nhắc việc, chi tiêu, danh mục, thành phố thời tiết, bản tin, đánh giá); gateway quên luôn người đó |
| `/zalodanhsach` | Xem danh sách tất cả tài khoản Zalo đã pair, kèm cách gọi đã đặt |

**Mỗi id Zalo được cách ly HOÀN TOÀN 1 bộ nhớ/ngữ cảnh chat riêng** (chat
session, trí nhớ dài hạn, ghi chú, nhắc việc, ngày nhớ, chi tiêu, danh mục cổ
phiếu, cảnh báo giá, thành phố thời tiết, bản tin sáng...) — KHÔNG chia sẻ với
người Zalo khác, và KHÔNG chia sẻ với chủ bot Telegram (`ALLOWED_USER_ID`),
kể cả người pair đầu tiên (admin đầu tiên qua `/pair <mã>`). Cơ chế: mỗi
`external_id` được cấp 1 `internal_user_id` (số nguyên ÂM, sinh 1 lần duy nhất
lúc pair, giữ nguyên vĩnh viễn cho tới khi `/zaloxoa`) dùng làm khoá lưu dữ
liệu — xem `channels/zalo_users.py`. Vì vậy nếu chủ bot vừa chat với trợ lý
qua Telegram vừa chat qua Zalo, đây là **2 ngữ cảnh tách biệt**, không tiếp
nối nhau (đánh đổi có chủ đích để tránh lộ bí mật giữa các kênh/người dùng).
Phân quyền admin/user riêng biệt chỉ kiểm soát việc CÓ được dùng lệnh nhóm hay
không (xem trên), không liên quan tới việc cách ly bộ nhớ này.

**Giới hạn lượt của thành viên Zalo** (`services/usage_quota.py`):

- Chỉ áp cho thành viên (role user). Chủ bot, admin Zalo và Zoom không bị giới hạn;
  lệnh chỉ đọc và `/huy` không bị đếm.
- Mỗi ngày tối đa `ZALO_MEMBER_DAILY_TURNS` lượt (mặc định 40) và
  `ZALO_MEMBER_DAILY_HEAVY` lượt nặng (mặc định 5), mở lại lúc 0h giờ VN. Sửa trên
  `/admin` → Người dùng & Zalo (kèm số lượt đã dùng hôm nay của từng người).
- Lượt nặng: `/anh`, `/tuvi_chitiet`, `/kymon_chitiet`, `/tsh_chitiet`, lượt chat
  có tra web, việc nền `/viec` và mỗi lần chạy tác vụ định kỳ. Hết lượt nặng thì vẫn trò chuyện được nhưng không tra web. Cổ phiếu
  tính như lượt thường.
- Hết lượt thì Lan Anh báo 1 câu theo cách gọi riêng kèm giờ mở lại, tối đa 1 lần
  mỗi giờ; các tin sau trong giờ đó im lặng. DB lỗi thì không chặn ai.

**Chỗ dành riêng cho chủ bot**: `OWNER_RESERVED_SLOTS=1` nghĩa là trong
`MAX_CONCURRENT_TURNS=2` luôn còn 1 chỗ cho Telegram/Zoom/web. Mọi lượt Zalo dùng
chung chỗ còn lại, kể cả admin và Zalo của chính chủ bot, nên 2 người Zalo không
chạy song song; gateway đặt `ZALO_DIRECT_CONCURRENCY=1` (đã có trong `render.yaml`)
để hàng đợi nằm ở gateway thay vì chờ trong Python quá 240 giây.

Lệnh quản lý/xem lại nhóm — dùng được từ Zalo (trực tiếp trong lúc chat với bot,
không phải gõ trong group), Zoom, hoặc Telegram (chỉ khi id_zalo gửi lệnh đó có
role=admin, riêng Telegram/Zoom luôn coi như đủ quyền vì chỉ có đúng 1 owner).
Các lệnh này đọc/ghi dữ liệu NHÓM (bảng `zalo_groups`/`zalo_group_messages`,
khoá theo `account_id`+`group_id`) — KHÔNG liên quan tới `internal_user_id` cá
nhân ở trên, nên không bị ảnh hưởng bởi việc cách ly bộ nhớ theo người dùng:

```text
/nhomzalo [từ khoá]  MỌI nhóm bot đang tham gia (kể cả chưa theo dõi): số thứ tự,
                       tên, số thành viên, group ID; ✅ đang theo dõi, 📣 nguồn Facebook
/themnhom <số|group_id> [alias]
/nhom
/xoanhom <group_id-or-alias>
/tongket <alias> <24h|7d|homnay|homqua>
/tongket on|off [alias] — bật/tắt tổng kết tự động hằng ngày (bỏ trống = mọi nhóm)
/dangnoi <alias> — xem nguyên văn thảo luận trong ngày hôm nay (không qua AI)
```

`/nhomzalo` gõ được từ Telegram, Zoom và Zalo (admin; thành viên thường nhận "Lệnh
này chỉ dành cho admin."). Python lấy danh sách từ control server của gateway
(`GET /groups`: `getAllGroups` rồi `getGroupInfo` theo lô 20 nhóm, tối đa 200 nhóm;
1 lượt quét tại 1 thời điểm, gõ lại/nhiều admin cùng gõ dùng chung lượt đang chạy,
mỗi người chờ tối đa 25s; 409 khi Zalo chưa đăng nhập) - `channels/zalo_group_list.py`.
Lọc theo tên không phân biệt dấu: `/nhomzalo gia dinh`. Số thứ tự đánh theo TOÀN BỘ
danh sách sắp theo tên (lọc vẫn giữ số gốc, các admin thấy cùng 1 số cho cùng 1 nhóm);
danh sách được giữ 60 phút (setting `zalo:group_pick:<account_id>`) để chọn bằng số:
`/themnhom 3` hoặc `/themnhom 3 giadinh`, `/fb_themnhom 3`. Số 1-3 chữ số luôn là số thứ tự (group ID thật dài 16-19 chữ số);
bỏ trống alias thì bot đặt theo tên nhóm bỏ dấu (`Gia Đình Nhỏ` → `gia-dinh-nho`),
nhóm đã theo dõi thì giữ alias cũ.

Đối với allowlist `/tongket`, gateway chỉ lưu text mới (kể cả chú thích của ảnh), không
backfill, không lưu media nhóm và không trả lời trong nhóm. Mỗi tin lưu kèm chữ của tin
được trả lời (reply, tối đa 600 ký tự - `/tongket`, `/dangnoi` hiện dạng `(↪ trả lời: "...")`)
và vai trò người gửi tại lúc nhắn (trưởng nhóm / phó nhóm / thành viên, xem mục tín hiệu
bên dưới). Tin chữ nhóm đi hàng đợi riêng (256 việc), không xếp sau việc tải ảnh của nhóm
nguồn Facebook; `/admin` có bộ đếm `group_messages`, `group_store_failures`. Thêm/xoá
nhóm từ Telegram/Zoom (`/themnhom`, `/xoanhom`, `/fb_themnhom`, `/fb_xoanhom`) báo gateway
nạp lại danh sách ngay (`POST /groups/refresh`), không chờ `ZALO_GROUP_REFRESH_MS`.
Luồng Facebook bên dưới có allowlist riêng và tải media riêng.

### Tín hiệu mua/bán cổ phiếu từ nhóm Zalo (`/tinhieu`)

Đọc tin các nhóm chứng khoán đang theo dõi, nhận ra ai gợi ý mua/bán mã nào ở nhóm nào,
rồi báo chủ bot qua **Zoom và Zalo** khoảng 2-3 phút sau khi có tin
(`services/zalo_signals.py`, `channels/zalo_signal_commands.py`, migration 031).

```text
/tinhieu                         tình trạng + tín hiệu hôm nay
/tinhieu on [nhóm] [admin|tatca] bật quét (mặc định chỉ trưởng/phó nhóm; bỏ trống = mọi nhóm)
/tinhieu off [nhóm]              tắt quét
/tinhieu nhom                    chế độ từng nhóm + người theo dõi thêm
/tinhieu nguoi <nhóm> <tên|id>   theo dõi thêm 1 người không phải admin (tên không dấu được)
/tinhieu bonguoi <nhóm> <tên|id> bỏ người đó
/tinhieu <mã> [homnay|7d|30d]    ai đã nói mua/bán mã đó, ở nhóm nào
/tinhieu top [homnay|7d|30d]     mã được nhiều người khác nhau nói nhất
/tinhieu homnay|homqua|7d        danh sách tín hiệu
/tinhieu nhip <phút>             nhịp quét 1-60 phút (mặc định 2; chậm thì đặt 30)
```

Nhóm phải được theo dõi trước bằng `/themnhom`. Dùng được từ Telegram, Zoom và Zalo
(admin), cùng quyền với `/tongket`. Telegram đã đủ 100 lệnh menu nên `/tinhieu` không có
trong menu gợi ý nhưng gõ vẫn chạy.

**Lọc admin nhóm.** Gateway lấy trưởng nhóm (`creatorId`) và phó nhóm (`adminIds`) bằng
`getGroupInfo` (bot không cần là admin), giữ trong RAM tối đa 200 nhóm, làm mới 30 phút/lần
và ngay khi Zalo báo đổi admin (`zalo-gateway/src/group-roles.ts`). Vai trò được lưu kèm
từng tin TẠI LÚC NHẮN. Chưa lấy được danh sách admin thì vai trò là "unknown" và tin đó
không được báo ở chế độ `admin` (không báo nhầm tin thành viên). Chế độ `tatca` nhận mọi
thành viên; `/tinhieu nguoi` thêm "chuyên gia" không làm admin vào chế độ `admin`.

**Ba lớp lọc, chống AI bịa:**

1. Code (không tốn AI): tin của người được theo dõi, có từ khoá mua/bán (mua, gom, múc,
   giải ngân, canh mua, bán, chốt lời, cắt lỗ, target...) VÀ có mã đang niêm yết (danh sách
   VCI cache 24h). Tin không có mã thì lấy mã từ tin được trả lời ("múc được" trả lời
   "HPG thế nào?").
2. AI trả JSON (`utility_json`: Groq trước, Gemini dự phòng), gom tối đa 8 tin/lượt theo
   nhóm, trần `ZALO_SIGNAL_MAX_AI_PER_HOUR` lượt/giờ. Phân loại mua/bán và gợi ý cho nhóm /
   tự báo đã mua-bán; bỏ câu hỏi, tin tức, nhận định chung, giả định, quảng cáo môi giới.
3. Code kiểm lại: mã phải là mã ứng viên của chính tin đó; giá chỉ giữ khi mọi con số có
   trong tin; câu trích phải là nguyên văn (không thì lấy đầu tin gốc). Người nêu, nhóm,
   giờ lấy từ tin gốc, không từ AI. Tin nhóm được rào như dữ liệu ngoài (`core.untrusted`).

Hết lượt AI hoặc AI lỗi thì con trỏ dừng ở tin chưa xử lý, lượt sau làm tiếp (một lô lỗi 3
lần liền thì bỏ lô đó để không chặn tin phía sau); tin cũ hơn `ZALO_SIGNAL_MAX_AGE_MIN` phút
(mặc định 120) thì bỏ. Lần đầu bật bot bắt đầu từ tin mới nhất, không quét lại lịch sử.

**Gửi.** 1 người - 1 mã - 1 hướng - 1 loại chỉ báo 1 lần mỗi ngày, kể cả khi người đó nói ở
nhiều nhóm (cùng lượt thì gộp 1 dòng ghi các nhóm; đã báo từ nhóm khác thì không báo lại -
`/tinhieu <mã>` và `top` vẫn đếm đủ từng nhóm). Mỗi lượt quét gộp tối
đa 8 tín hiệu thành 1 tin cho mỗi kênh, qua `notify_hub` nguồn `zalo_signal` (URGENT: gửi
ngay, trong giờ yên lặng thì giữ tới sáng):

- Zoom: jid đã ghép đôi; Zoom tắt hoặc chưa ghép đôi thì về Telegram.
- Zalo: tài khoản Zalo chủ bot (controller), tôn trọng `/yenlang` riêng của tài khoản đó.

Mỗi kênh đánh dấu đã giao riêng, nhận nguyên tử (2 instance chạy chồng lúc deploy không
gửi đôi); gửi lỗi thì thử lại trong 6 giờ. Tin báo ghi người nêu + vai trò, nhóm, giờ,
vùng mua/mục tiêu/cắt lỗ nếu có, câu trích, "🔥 N người ở M nhóm cùng nói mua X" khi từ 2
người, "⭐ đang có trong danh mục", và dòng miễn trừ: đây là ý kiến thành viên nhóm, không
phải khuyến nghị của Lan Anh (bot không chấm điểm hay đổi tín hiệu; phân tích của bot vẫn
do `stock/policy.py` quyết định khi gõ lệnh phân tích).

Biến môi trường (đều có mặc định): `ZALO_SIGNALS` (0 = tắt hẳn), `ZALO_SIGNAL_INTERVAL_MIN`
(2), `ZALO_SIGNAL_MAX_AI_PER_HOUR` (30), `ZALO_SIGNAL_MAX_AGE_MIN` (120),
`ZALO_SIGNAL_CHANNELS` (`zoom,zalo`), `ZALO_SIGNAL_RETENTION_DAYS` (90).

Giới hạn: không đọc chữ trong ảnh biểu đồ (chỉ đọc chú thích); tin chỉ có giá mà không có
mã (mã nằm ở tin trước đó, không phải tin được trả lời) thì bỏ qua; tin đến lúc gateway
mất kết nối không được lấy lại.

### Zalo → Facebook Page (độc lập `/tongket`)

Luồng Facebook dùng danh sách `zalo_facebook_groups` riêng, không dùng
`zalo_groups` của `/tongket`. Một nhóm có thể nằm ở một hoặc cả hai danh sách;
thêm/xóa ở luồng Facebook không thay đổi dữ liệu tổng kết nhóm.

#### Cấu hình 1 hoặc nhiều Facebook Page

Page mặc định (`"default"`):

```env
FACEBOOK_PAGE_ID=...
FACEBOOK_PAGE_ACCESS_TOKEN=...
FACEBOOK_GRAPH_VERSION=v26.0   # tùy chọn
```

Muốn đăng thêm Page thứ 2 trở đi, thêm hậu tố `_<page_key>` (key tuỳ chọn,
ví dụ `2`, `3`...):

```env
FACEBOOK_PAGE_ID_2=...
FACEBOOK_PAGE_ACCESS_TOKEN_2=...
```

`/fb_pages` liệt kê mọi page_key đang được cấu hình đầy đủ (có cả ID lẫn
access token). **Không cần gán page cho từng nhóm Zalo** — mọi nhóm nguồn đều
dùng chung tập page này khi đăng.

**Lọc bài từ nhóm Zalo trước khi vào hàng chờ Facebook** (`channels/facebook_intake.py`,
`services/facebook_caption.skip_reason`). Gateway gộp ảnh + chữ liên tiếp của cùng một người
gửi thành một bài (chờ tối đa `ZALO_FB_MERGE_WINDOW_MS`, mặc định 30 giây kể từ tin cuối) và
tự tách khi người bán đăng liên tục nhiều sản phẩm: caption mới mở bài mới; với kiểu "ảnh trước
rồi caption", ảnh mới sau một bài đã đủ ảnh + chữ cũng mở bài mới. Ảnh tải lỗi được thử lại 1 lần.
Bài bị bỏ (trả 204, ghi log "Bỏ qua bài Zalo (<lý do>)") khi:

- **Có ảnh nhưng không có caption**: chỉ có ảnh, hoặc ảnh + link, hoặc ảnh + vài chữ ngắn như
  "Link mua:" (caption cần từ 10 chữ/số trở lên, không tính link kể cả link không có `https://`).
- **Có caption nhưng không có ảnh** (kể cả khi gateway không tải được ảnh sau 2 lần thử).
- **Không phải bài bán hàng**: thành viên hỏi xin link ("ai có link", "cho mình xin link"), tuyển
  CTV/việc làm online, vay tiền, cờ bạc - CHỈ khi bài không có giá và không có link Shopee (bài bán
  hàng thật kèm "nhận sỉ, tuyển CTV" ở cuối vẫn giữ). Số tiền kiểu "thu nhập 10-15tr" không tính là giá.
- **Chỉ báo mã giảm giá/săn deal**: nhắc "lưu mã", "mã giảm", "deal VIP"... mà không có giá sản
  phẩm, dưới 400 ký tự. Giá nhận cả dạng "159k", "1tr2", "1.299.000", "199.000đ"; con số là điều
  kiện của mã ("đơn 0đ", "tối đa 50k", "max 30k", "hoàn xu 15%") không tính là giá. Bài có link
  Shopee trỏ thẳng tới một sản phẩm (`...-i.<shop>.<item>`, `/product/<shop>/<item>`) luôn giữ.
  Nhận cả bài "canh mã/back" theo khung giờ: "15H BACK SVIP 25% MAX 999K/300K", "15H CANH BACK
  MÃ TRENDY", "BACK MÃ BÁCH HÓA", "LOẠT MÃ EXTRA", "Mã FB 30%", "ÁP TOÀN SÀN"; vế sau dấu "/"
  ("999K/300K") là đơn tối thiểu, không tính là giá.
- **Trùng lặp - so chung MỌI nhóm nguồn, không chỉ trong một nhóm**: một deal được chia sẻ ở
  nhiều nhóm chỉ vào hàng chờ 1 lần. So với bài trong `FACEBOOK_DEDUP_DAYS` ngày (mặc định 7, kể
  cả bài đã đăng/bỏ qua/đã bị dọn): cùng nội dung (bỏ dấu, link, ký tự đặc biệt); cùng sản
  phẩm/short-link Shopee; chữ gần giống và cùng giá; bài đăng lại có thêm/bớt vài dòng chào hỏi,
  hashtag, lời mời vào nhóm (85% chữ của bài ngắn nằm trong bài kia) và giá khớp; ảnh trùng (chịu
  được ảnh bị Zalo nén lại khi chuyển tiếp) và chữ khá giống; cùng người bán đăng lại ảnh + giá ở
  nhóm khác; hoặc toàn bộ ảnh (từ 2 ảnh) đều trùng. Ảnh trùng một mình không tính (người bán hay
  dùng chung banner); cùng mẫu chữ nhưng khác giá là deal mới. Giá đọc được dạng "159k", "1tr2",
  "1.299.000", "199.000đ". Trùng chính xác nội dung/sản phẩm được tra bằng index trên cả cửa sổ
  ngày; so "gần giống" xét `FACEBOOK_DEDUP_SCAN_LIMIT` bài gần nhất (mặc định 800), chạy trong
  thread. Bài gốc ghi lại số lần bị đăng lại và các nhóm đã đăng lại nó (xem ở `/fb_xem`). Xem
  `services/facebook_dedup.py`.

Tắt từng bộ lọc trên Render: `FACEBOOK_REQUIRE_PHOTO_AND_CAPTION=0` (cho phép bài thiếu ảnh/caption),
`FACEBOOK_SKIP_VOUCHER_POSTS=0` (giữ bài mã giảm giá), `FACEBOOK_SKIP_NON_SALE_POSTS=0` (giữ bài hỏi
đáp/tuyển dụng). Dùng `/fb_boloc` để xem bộ lọc đã giữ/bỏ bao nhiêu bài, lý do, **theo từng nhóm
nguồn** (vào hàng chờ / trùng / trùng nhóm khác / bỏ) và các bài bị bỏ gần nhất (`/fb_boloc reset`
để đếm lại). `/fb_loclai` chạy lại bộ lọc cho bài đang chờ và bỏ các bài trùng một bài cũ hơn ở
bất kỳ nhóm nào.

**Tên nhóm thay cho group ID.** Bản xem trước ("Nhóm: Săn Sale Giá Rẻ"), `/fb_nhom`, `/fb_themnhom`,
`/fb_xoanhom`, `/fb_loclai`, `/fb_boloc` hiện tên thật của nhóm Zalo. Tên được lưu khi `/fb_themnhom
<số>`, cập nhật mỗi lần xem `/nhomzalo` (nhóm đổi tên), và tự hỏi gateway khi còn nhóm chưa có tên
(tối đa 1 lần/6 giờ). `/fb_xoanhom` nhận tên gợi nhớ, tên nhóm (không cần dấu) hoặc group ID.

**Dung lượng Supabase (free tier 500 MB).** Ảnh được nén trước khi lưu (cạnh dài tối đa 2048px,
JPEG, bỏ EXIF) và bị xoá ngay khi bài đăng xong hoặc bị `/fb_boqua`. Hàng chờ giữ tối đa
`FACEBOOK_MAX_PENDING` bài chờ duyệt (mặc định 150): vượt ngưỡng thì tự xoá bài chờ **cũ nhất**
và báo trong tin xem trước (không đụng bài lỗi hoặc đã đăng một phần). Bài đã đăng/bỏ qua cũ hơn
`FACEBOOK_HISTORY_DAYS` ngày (mặc định 30) được dọn tự động, tối đa 1 lần/giờ.

**Tự thu hồi dung lượng (VACUUM FULL).** Xoá ảnh chỉ tạo chỗ trống để Postgres tái dùng; con số
"Database size" trên Supabase chỉ giảm sau `VACUUM FULL`. Job `services/db_maintenance.py` tự chạy
lệnh này cho `facebook_post_media` tối đa 1 lần/tuần (mặc định 3h sáng Chủ nhật giờ VN) và CHỈ khi:
lãng phí >= 50 MB và >= một nửa bảng, không có bài đang ở trạng thái POSTING, và DB cộng bản chép
tạm vẫn dưới 95% `DB_SIZE_LIMIT_MB`. Lấy khoá tối đa 5 giây, không được thì bỏ lượt. Kết quả trước/sau
báo qua Telegram. Tắt bằng `DB_AUTO_VACUUM_FULL=0`. Xem trước quyết định: `GET /admin/api/db-usage`.

**AI viết lại bài** khi đã đủ link affiliate: giọng người bán kể cho bạn bè, tránh văn mẫu
("siêu phẩm", "không thể bỏ lỡ"...), tối đa 2-3 emoji, không Markdown. Bản viết lại phải giữ đúng
mọi con số của bài gốc; sai/thiếu thì hỏi lại AI 1 lần, vẫn sai thì giữ bài gốc đã lọc link. Câu
dẫn xuống bình luận và câu mở đầu bình luận link đổi theo từng bài.

#### Page riêng cho chứng khoán + tin CafeF (`market_page`)

Hai luồng tự động chuyển từ n8n sang `services/market_page.py` đăng lên một Page
**tách hẳn** khỏi Page Shopee: báo cáo VN-INDEX/DNSE (15:20 kết phiên, thứ 2-6, kèm
biểu đồ) và bản tin CafeF (08:30 hằng ngày, kèm comment bài nổi bật). Cấu hình bằng key
`MARKET`:

```env
FACEBOOK_PAGE_ID_MARKET=...
FACEBOOK_PAGE_ACCESS_TOKEN_MARKET=...
```

Page này không xuất hiện trong `/fb_pages` và `/fb_ok` không đăng bài Shopee lên đó.
Bỏ trống 2 biến là tắt luồng. Nội dung do AI chain của bot sinh (không cần Gemini key riêng).

- **Giờ đăng** (giờ VN, `HH:MM`, nhiều mốc cách nhau bằng dấu phẩy): `MARKET_STOCK_TIMES_VN`
  (mặc định `15:20` - kết phiên, chỉ thứ 2-6) và `MARKET_NEWS_TIMES_VN` (mặc định `08:30`, hằng ngày).
  Đổi giá trị trong Render rồi deploy lại; giá trị sai định dạng thì dùng mặc định.
- **Chạy thủ công**: `/fb_market <stock|news>` chỉ tạo nội dung để xem thử (không đăng);
  `/fb_market <stock|news> dang` đăng thật. Dùng được từ Telegram, Zalo admin và Zoom.
  Trang `/admin` có mục "Page chứng khoán & tin CafeF" với các nút tương ứng, kèm lịch và
  kết quả lần chạy gần nhất.
- **Tuân thủ**: prompt cấm khuyến nghị mua/bán/tỷ trọng/giá mục tiêu; mỗi bài tự gắn dòng
  nguồn (DNSE, CafeF) và lời miễn trừ trách nhiệm. Bộ lọc bắt cả câu khuyến nghị "mềm" ("có thể
  canh mua", "ưu tiên giải ngân", "chốt lời một phần", "vùng mua"...) nhưng không chặn mô tả
  thị trường ("khối ngoại bán ròng", "bán tháo", "ngân hàng giải ngân tín dụng"). Gặp khuyến
  nghị thì AI được viết lại 1 lần, vẫn còn thì bài bị bỏ.
- **Không đăng sai/trùng**: mỗi phiên giao dịch chỉ đăng 1 lần, kể cả đã đăng tay bằng
  `/fb_market stock dang`; ngày nghỉ lễ (DNSE không có phiên hôm nay) không đăng lại phiên cũ.
  Bản tin chỉ dùng tin CafeF trong 24 giờ qua (cần ít nhất 3 tin) và bỏ qua nếu phần lớn tin
  trùng bản tin trước.
- **Chạy bù và thử lại**: lỡ giờ do Render restart, hoặc AI/DNSE lỗi, thì lịch thử lại mỗi
  `MARKET_RETRY_MIN` phút (mặc định 10), tối đa `MARKET_MAX_ATTEMPTS` lần (mặc định 4), trong
  `MARKET_CATCHUP_MIN` phút kể từ giờ đăng (mặc định 120).
- **Số liệu**: RSI theo Wilder (khớp TradingView/app chứng khoán); số mã tăng/giảm ghi rõ là
  "trong nhóm cổ phiếu theo dõi", không phải toàn thị trường; số định dạng kiểu Việt Nam; biểu
  đồ ghi ngày dd/mm.
- **Ảnh**: biểu đồ VN-INDEX và ảnh bài CafeF nổi bật được gắn khung trắng + logo giống ảnh luồng
  Zalo → Facebook. `MARKET_NEWS_IMAGE=none` để đăng bản tin không kèm ảnh CafeF (an toàn nhất về
  bản quyền ảnh). Comment bài nổi bật chỉ tóm tắt ngắn 80-120 từ kèm link đọc bài gốc.

Các lệnh `/fb_*` dùng được từ **Zalo admin, Telegram owner và Zoom jid đã pair**:

```text
/fb_nhom                              # nhóm nguồn theo TÊN nhóm Zalo
/fb_pages
/fb_themnhom <số|group_id> [alias]   # số thứ tự lấy từ /nhomzalo
/fb_xoanhom <tên gợi nhớ|tên nhóm|group_id>
/fb_loclai                            # lọc lại hàng chờ + bỏ bài trùng giữa các nhóm
/fb_boloc [reset]                     # thống kê bộ lọc theo từng nhóm
/fb_xem <post_id>
/fb_sua <post_id> <nội dung mới>
/fb_link <post_id> <affiliate_url>  # bài chỉ có 1 link Shopee
/fb_link <post_id> <source_url> <affiliate_url>  # từng link khi bài có nhiều link
/fb_ok <post_id>
/fb_check <post_id>  # kiểm tra từng page đã published/công khai và lấy permalink
/fb_boqua <post_id>
/fb_reset xacnhan   # thiếu "xacnhan" thì chỉ cảnh báo kèm số bài đang chờ
```

Quy trình vận hành:

1. Từ Zalo admin, Telegram hoặc Zoom gõ `/nhomzalo` để xem nhóm bot đang ở.
2. Gõ `/fb_themnhom <số> [alias]` (số thứ tự trong danh sách vừa xem, hoặc
   `/fb_themnhom <group_id> <alias>`).
3. Gateway tự thu text + ảnh mới của nhóm đó và gom các message liên tiếp của
   cùng người gửi thành một bài chờ duyệt (ảnh gửi sang Python dạng nhị phân,
   `POST /internal/zalo/facebook-group-post-multipart`). Bài vào hàng chờ ngay,
   phần AI viết lại + gửi xem trước chạy nền nên gateway không phải chờ. Luồng này
   không ghi vào dữ liệu `/tongket` trừ khi nhóm đó cũng được thêm riêng bằng `/themnhom`.
4. Preview có `post_id` và các link Shopee gốc chưa chuyển đổi để copy. Bot
   gửi preview tới Zalo admin, Telegram owner và Zoom jid đã pair (nếu Zoom
   đang bật); cũng có thể xem lại bằng `/fb_xem <post_id>`. Trên Telegram,
   preview có nút [✅ Duyệt] [🗑 Bỏ qua] (chạy đúng `/fb_ok`, `/fb_boqua`; nút được
   gỡ sau khi bấm).
5. Nếu có Shopee link, tự tạo short-link `https://s.shopee.vn/...` trên Shopee
   Affiliate rồi nhập: `/fb_link <post_id> <affiliate_url>` (1 link) hoặc
   `/fb_link <post_id> <source_url> <affiliate_url>` (bài có nhiều link, mỗi link một
   lệnh). Bot chỉ nhận host `s.shopee.vn` và không gọi Shopee để chuyển đổi.
   Khi mọi link của bài đã có affiliate, AI tự viết lại bài **không chứa link**; câu
   "Chi tiết ưu đãi và link sản phẩm mình để ở bình luận đầu tiên nha cả nhà." được
   nối cuối bài, còn link affiliate được thả vào bình luận đầu tiên của từng Page.
6. Có thể sửa caption bằng `/fb_sua <post_id> <nội dung mới>` (link Shopee trong
   caption luôn bị lọc khỏi bài đăng). Ảnh được gắn viền mỏng + logo `assets/lcta_logo.png` ở góc
   dưới phải khi đăng; ảnh gốc trong hàng đợi không bị đổi.
7. Gõ `/fb_ok <post_id>` để đăng — bot đăng **lần lượt lên TẤT CẢ Facebook
   Page đang được cấu hình** (xem `/fb_pages`); nếu vẫn còn Shopee link chưa
   có affiliate thì bot từ chối đăng cho cả loạt. Kết quả từng page được báo
   riêng (gồm cả việc thả bình luận link; nếu bình luận lỗi, `/fb_ok` chạy lại chỉ thử lại
   bình luận, không tạo lại bài; token Page cần quyền `pages_manage_engagement`): page đăng thành công hiện Post ID + link, page lỗi hiện lý do lỗi.
   Bài vẫn ở trạng thái chờ nếu còn ít nhất 1 page lỗi — gõ lại `/fb_ok
   <post_id>` để bot **chỉ thử lại đúng (các) page đã lỗi lần trước**, các
   page đã đăng thành công sẽ không bị đăng lại/không bị đụng tới. Dùng
   `/fb_boqua <post_id>` để bỏ bài. `/fb_reset xacnhan` xóa toàn bộ bài Facebook đã lưu
   của tài khoản hiện tại; nếu không còn bài của tài khoản khác, ID bài mới sẽ
   bắt đầu lại từ `#1`.

Telegram và Zoom chỉ là kênh quản trị/duyệt; việc thu thập bài nhóm Zalo vẫn do
`zalo-gateway` thực hiện. `account_id` cho các lệnh `/fb_*` trên Telegram/Zoom
được lấy từ Zalo session đang đăng nhập, nên không cần có nhóm `/tongket` trước.

## Zoom Team Chat

Kênh Zoom hoạt động theo cùng mô hình 1-chủ như Telegram: chỉ ĐÚNG 1 jid Zoom
được pair mới nói chuyện được với bot.

1. Tạo app trên [Zoom Marketplace](https://marketplace.zoom.us/) (loại **Chatbot**),
   bật **Event Subscriptions**, trỏ Bot Endpoint URL tới
   `<WEBHOOK_BASE_URL>/webhook/zoom`.
2. Lấy `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`, `ZOOM_BOT_JID` (mục Feature > Chatbot),
   `ZOOM_SECRET_TOKEN` (mục Access > Token > Secret Token) và `ZOOM_ACCOUNT_ID`.
3. Set `ZOOM_ENABLED=true` cùng các biến trên, deploy lại.
4. Bấm **Validate** trên Marketplace để bot trả lời challenge-response
   (`endpoint.url_validation`, tự động xử lý trong `web.py`).
5. Nhắn thử cho bot qua Zoom — vì chưa pair, chủ bot sẽ nhận cảnh báo kèm jid
   qua Telegram. Gõ `/zoompair <jid> [tên]` trên Telegram để cấp quyền.
6. Dùng `/zoomstatus` để xem đang pair jid nào, `/zoomxoa` để gỡ.

Lưu ý: tên field chính xác trong webhook payload Zoom (`userJid`, `toJid`,
`cmd`...) có thể khác nhau tùy loại app/version sự kiện — dùng tính năng gửi
sự kiện thử trên Marketplace để xác nhận, chỉnh lại `channels/zoom.py::parse_event`
nếu cần trước khi deploy thật.

Mọi lệnh CÁ NHÂN (`/prompt`, `/gia`, `/dich`, `/reset`, `/history`, `/memory`,
`/forget`, `/notes`, `/nhac`, `/chitieu`, `/vang`, `/kymon`...) và lệnh quản trị
(`/model`, `/status`, `/thongke`, `/userouter9` - trên Zalo chỉ admin) hoạt động
GIỐNG HỆT trên Zalo và Zoom vì cả hai đều đi qua chung
`services/channel_chat_service.py` và `services/channel_command_service.py`. Zoom
dùng chung dữ liệu chủ bot với Telegram (nhắc việc, chi tiêu, trí nhớ); `/huy` và
lệnh chỉ đọc trên Zoom cũng đi làn nhanh ở webhook khi lượt khác đang chạy.

Các lệnh QUẢN LÝ/XEM LẠI NHÓM ZALO (`/nhom`, `/themnhom`, `/xoanhom`,
`/tongket`, `/dangnoi`) cũng dùng được **từ cả Zoom lẫn Telegram**, không chỉ
Zalo — dữ liệu nhóm luôn là dữ liệu Zalo (thu thập thụ động qua
`zalo-gateway`, tài khoản Zalo bot đăng nhập thật), Zoom/Telegram chỉ là kênh
khác để TRUY VẤN dữ liệu đó (`channels/group_commands.py::maybe_handle_group_command`
dùng chung cho cả 3 kênh). Trên chính kênh Zalo, chỉ tài khoản có role=admin
mới gọi được các lệnh này (xem mục "Zalo — nhiều tài khoản & phân quyền");
trên Zoom/Telegram không có khái niệm role riêng vì mỗi kênh chỉ có đúng 1
owner được phép dùng, nên luôn coi như đủ quyền. Vì Zoom/Telegram không có sẵn
`account_id` Zalo đi kèm request như kênh Zalo (`channels/router.py` nhận
trực tiếp từ payload bridge), bot ưu tiên lấy `accountId` từ Zalo session đang
đăng nhập; với session cũ không có field này mới fallback về
`channels/zalo_repository.py::resolve_default_account_id()`. Thiết kế này vẫn
giả định chỉ có 1 tài khoản Zalo BOT/B đang active.

Zoom/Telegram **không thể tự thu thập tin nhắn nhóm Zalo** (đó vẫn là việc
của `zalo-gateway`) — chúng chỉ đọc lại dữ liệu Zalo đã có sẵn. Bản thân kênh
Zoom cũng không đọc thụ động được tin nhắn trong Zoom channel (chỉ nhận tin
gửi trực tiếp/@mention cho bot), nên KHÔNG có `/tongket`/`/dangnoi` tương
đương CHO NHÓM ZOOM — chỉ dùng được để xem lại nhóm ZALO.

Các lệnh Facebook (`/fb_nhom`, `/fb_themnhom`, `/fb_xoanhom`, `/fb_xem`,
`/fb_sua`, `/fb_link`, `/fb_ok`, `/fb_boqua`, `/fb_reset`) cũng dùng được từ Zoom với cùng
quyền admin như Zalo admin. Zoom dùng Zalo session hiện tại để xác định
`account_id`, nên luồng Facebook không phụ thuộc việc có cấu hình `/tongket`.

## Kiểm tra bài Facebook có thực sự công khai

Sau khi `/fb_ok <post_id>`, bot không còn chỉ tin vào HTTP 200/Post ID từ Graph API.
Bot tạo bài với `published=true`, đọc lại `is_published`, `is_hidden`,
`timeline_visibility`, `permalink_url` và kiểm tra `/{page-id}/published_posts`.

Nếu bot trả `🌐 Graph API xác nhận...`, bài đã được Graph API xác nhận là post
published bình thường. Bot cũng trả `Link bài`; nên mở link đó bằng tài khoản Facebook
khác hoặc cửa sổ ẩn danh để kiểm tra trải nghiệm công khai thực tế.

Nếu bài đã đăng trước đó nhưng không thấy trong tab Posts, dùng:

```text
/fb_check <post_id>
```

Nếu `is_published=true` nhưng `in_published_posts=false` hoặc
`timeline_visibility=hidden`, không nên đăng lại ngay vì có thể tạo bài trùng. Hãy dùng
permalink mà bot trả về để kiểm tra từ tài khoản khác và xem Render log/Graph status trước.
Facebook có thể cập nhật tab Posts chậm hơn Photos/Feed trong một số thời điểm.
