# Lananh — Trợ lý AI cá nhân (Telegram · Zalo · Zoom)

MVP một người dùng, chạy trên nền Gemini. Hoạt động trên Telegram và tùy chọn
Zalo/Zoom, dùng chung provider chain Gemini, trí nhớ dài hạn, công cụ, nhắc
việc và phân tích cổ phiếu Việt Nam.

## Trạng thái MVP

Repository được thiết kế cho **một chủ sở hữu**:

- Telegram chỉ chấp nhận `ALLOWED_USER_ID`.
- Zalo hỗ trợ **nhiều tài khoản** cùng nhắn cho 1 bot, phân quyền admin/thành viên,
  **mỗi tài khoản 1 bộ nhớ/ngữ cảnh chat riêng** (không lộ bí mật giữa các bên).
- Zoom Team Chat ghép đôi (pairing) đúng 1 jid được phép nói chuyện với bot.
- Telegram, Zalo và Zoom dùng chung memory, tools và stock pipeline.
- Session/token nhạy cảm được mã hóa trước khi lưu database.
- Webhook và background tasks được drain khi shutdown.
- Backtest nặng bị chặn trên Render Web Service.
- Python và TypeScript có lint, format, test và CI.

Đây không phải hệ thống multi-tenant, nền tảng tư vấn tài chính được cấp phép hoặc broker đặt lệnh.

## Tính năng

### Trợ lý chung

- Provider chain: 9Router (gateway OpenAI-compatible) → Groq (miễn phí) → OpenRouter (miễn phí) → AI Studio key 1 → key 2.
- Tự cooldown provider hết quota và probe lại 9Router.
- Tác vụ cần Google Search thật (`require_real_search`) dùng riêng 1 chuỗi: Groq `compound-mini` (tool search tích hợp, miễn phí) → Gemini grounding (API key 1/2) - bỏ qua 9Router và OpenRouter vì không đảm bảo có tool search thật.
- Lịch sử theo phiên và trí nhớ dài hạn trên Supabase Postgres.
- Ghi chú, reminder và facts danh mục qua ngôn ngữ tự nhiên.
- Tìm giá sản phẩm bằng grounded search chính thức.
- Phân tích ảnh và tạo prompt.
- Thống kê lượt gọi AI theo user/kênh và theo model (`/thongke` trên chat, hoặc trang `/admin`).

### Telegram

- Long polling khi chạy local; webhook khi deploy Render.
- Khóa truy cập theo một Telegram user ID.
- Xử lý text dài, ảnh và image document.
- Lệnh quản lý memory, provider, model và Zalo.

### Zalo

- Gateway `zca-js` tùy chọn chạy cạnh Python service.
- Đăng nhập tài khoản bot bằng QR từ Telegram.
- **Nhiều tài khoản Zalo** có thể nhắn cho bot, mỗi tài khoản có role riêng
  (`admin` hoặc `user`) và có thể khóa/mở khóa/xóa độc lập — xem mục
  "Zalo — nhiều tài khoản & phân quyền" bên dưới.
- Người ghép đôi ĐẦU TIÊN (qua mã `/pair`) tự động là admin đầu tiên; owner
  Telegram cấp quyền thêm cho người khác qua `/zalopair`, `/zaloadmin`.
- Chỉ **admin** dùng được lệnh quản lý/xem lại nhóm (`/nhom`, `/themnhom`,
  `/xoanhom`, `/tongket`, `/dangnoi`); **thành viên** (`role=user`) chỉ chat và
  dùng các lệnh cá nhân bình thường (`/prompt`, `/gia`, `/dich`, `/reset`...).
- Thu thập text từ nhóm allowlist, tạo summary và gửi qua durable outbox.
- Tổng kết hằng ngày theo `Asia/Ho_Chi_Minh`.

### Phân tích cổ phiếu Việt Nam

`stock/` tách rõ data, validation, features, policy và presentation:

```text
DNSE ──┐
       ├─ OHLCV contract ─ features ─ deterministic policy ─ report
VCI ───┘                         │
                         VNINDEX / ngành / cơ bản / tin tức
```

Năng lực hiện tại:

- DNSE OHLCV với failover tự động sang `vnstock`/VCI.
- Strict contract cho độ dài mảng, số hữu hạn, quan hệ OHLC và ngày giao dịch.
- RSI, MACD, MA/EMA, Bollinger, ADX, ATR, Donchian, thanh khoản, distribution days và key levels.
- Gate theo market regime, data quality, setup và risk/reward.
- `BUY`, `HOLD`, `WATCH`, `SELL`, `NO_TRADE` do code quyết định; LLM chỉ diễn giải.
- Vùng mua, stop, target, R:R, position sizing và kịch bản bull/base/bear.
- Fundamental theo ngành: ưu tiên P/B cho ngân hàng, chứng khoán, bảo hiểm và bất động sản; P/E ở nhóm phù hợp.
- Walk-forward backtest có phí, thuế bán, slippage, T+, và 30% out-of-sample.

Bot không kết nối tài khoản chứng khoán và không đặt lệnh.

## Kiến trúc

```text
Telegram webhook ─┐
                  ├─ FastAPI / shared services ─ Gemini provider chain
Zalo gateway ─────┘              │
                                 ├─ memory và tools
                                 ├─ stock research
                                 └─ Supabase Postgres

Render Docker service
├── Uvicorn: webhook, bridge, scheduler và assistant
└── Node.js: Zalo listener và loopback control server
```

Zalo control server chỉ bind `127.0.0.1:9901`.

## Yêu cầu

- Python 3.12
- Node.js 18+
- Telegram bot token
- Supabase Postgres Session Pooler URL
- API key 9Router (gateway OpenAI-compatible)
- Fernet encryption key
- Tùy chọn: Groq API key, OpenRouter API key, Google AI Studio keys, tài khoản Zalo bot riêng, app Zoom Marketplace

## Cấu hình bắt buộc

| Biến | Mục đích |
|---|---|
| `TELEGRAM_TOKEN` | Token từ BotFather |
| `ALLOWED_USER_ID` | Telegram user duy nhất được phép dùng bot |
| `ROUTER9_API_KEY` | API key 9Router (provider AI đầu tiên trong chain) |
| `DATABASE_URL` | Supabase Session Pooler URL |
| `SETTINGS_ENC_KEY` | Fernet key mã hóa settings nhạy cảm |
| `WEBHOOK_SECRET` | Secret cho Telegram webhook trên Render |
| `WEBHOOK_BASE_URL` | Public base URL; Render có thể cung cấp `RENDER_EXTERNAL_URL` |

Tạo secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`SETTINGS_ENC_KEY` là bắt buộc và fail closed. Không đổi hoặc làm mất key sau khi đã lưu ciphertext.

Xem `.env.example` và `render.yaml` để biết toàn bộ biến tùy chọn.

## Chạy local

```bash
git clone https://github.com/<your-username>/lananh.git
cd lananh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python main.py
```

Docker:

```bash
docker build -t lananh .
docker run --env-file .env -p 10000:10000 lananh
```

## Deploy Render

1. Tạo Supabase và lấy **Session Pooler** connection string.
2. Tạo Telegram bot và lấy Telegram user ID của chủ sở hữu.
3. Tạo Render Blueprint từ repository.
4. Điền secret trong `render.yaml`.
5. Deploy lần đầu với `ZALO_ENABLED=false`.
6. Kiểm tra `/` trả `200` và Telegram hoạt động.
7. Chỉ bật Zalo sau khi webhook ổn định.

Render đặt cứng:

```env
STOCK_BACKTEST_ALLOW_ON_RENDER=false
```

Nếu code vô tình gọi backtest trên Render, tác vụ dừng trước khi tải dữ liệu hoặc dùng CPU đáng kể. Hãy tạo `stock/data/backtest_stats.json` bằng job CI/offline riêng rồi deploy file kết quả.

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
  KHÔNG dùng được lệnh nhóm, dù có gõ đúng cú pháp cũng bị bỏ qua như tin nhắn
  không hỗ trợ.

Khi 1 tài khoản Zalo CHƯA được cấp quyền nhắn cho bot, bot **im lặng** với
người đó (không lộ ra là bot chưa cấu hình xong) và chỉ âm thầm báo owner qua
Telegram kèm id_zalo. Owner dùng các lệnh sau (chỉ trên Telegram) để quản lý:

| Lệnh | Chức năng |
|---|---|
| `/zalopair <id_zalo> [tên]` | Cấp quyền THÀNH VIÊN (chỉ tính năng bình thường) |
| `/zaloadmin <id_zalo> [tên]` | Cấp/nâng quyền ADMIN (dùng được lệnh nhóm) — hỗ trợ nhiều admin |
| `/zalohaquyen <id_zalo>` | Hạ 1 admin về thành viên thường |
| `/zalokhoa <id_zalo>` | Khóa 1 tài khoản (không chat được nữa) |
| `/zalomokhoa <id_zalo>` | Mở khóa |
| `/zaloxoa <id_zalo>` | Xóa pairing hẳn |
| `/zalodanhsach` | Xem danh sách tất cả tài khoản Zalo đã pair |

**Mỗi id Zalo được cách ly HOÀN TOÀN 1 bộ nhớ/ngữ cảnh chat riêng** (chat
session, trí nhớ dài hạn, ghi chú, danh mục cổ phiếu...) — KHÔNG chia sẻ với
người Zalo khác, và KHÔNG chia sẻ với chủ bot Telegram (`ALLOWED_USER_ID`),
kể cả người pair đầu tiên (admin đầu tiên qua `/pair <mã>`). Cơ chế: mỗi
`external_id` được cấp 1 `internal_user_id` (số nguyên ÂM, sinh 1 lần duy nhất
lúc pair, giữ nguyên vĩnh viễn cho tới khi `/zaloxoa`) dùng làm khoá lưu dữ
liệu — xem `channels/zalo_users.py`. Vì vậy nếu chủ bot vừa chat với trợ lý
qua Telegram vừa chat qua Zalo, đây là **2 ngữ cảnh tách biệt**, không tiếp
nối nhau (đánh đổi có chủ đích để tránh lộ bí mật giữa các kênh/người dùng).
Phân quyền admin/user riêng biệt chỉ kiểm soát việc CÓ được dùng lệnh nhóm hay
không (xem trên), không liên quan tới việc cách ly bộ nhớ này.

Lệnh quản lý/xem lại nhóm — dùng được từ Zalo (trực tiếp trong lúc chat với bot,
không phải gõ trong group), Zoom, hoặc Telegram (chỉ khi id_zalo gửi lệnh đó có
role=admin, riêng Telegram/Zoom luôn coi như đủ quyền vì chỉ có đúng 1 owner).
Các lệnh này đọc/ghi dữ liệu NHÓM (bảng `zalo_groups`/`zalo_group_messages`,
khoá theo `account_id`+`group_id`) — KHÔNG liên quan tới `internal_user_id` cá
nhân ở trên, nên không bị ảnh hưởng bởi việc cách ly bộ nhớ theo người dùng:

```text
/nhomzalo            (chỉ admin ĐẦU TIÊN/controller dùng được - liệt kê MỌI
                       nhóm bot đang tham gia, kể cả chưa thêm vào allowlist)
/themnhom <group_id> <alias>
/nhom
/xoanhom <group_id-or-alias>
/tongket <alias|all> <24h|7d|homnay|homqua>
/dangnoi <alias> — xem nguyên văn thảo luận trong ngày hôm nay (không qua AI)
```

Gateway chỉ lưu text mới từ nhóm allowlist, không backfill, không lưu media nhóm và không trả lời trong nhóm.

## Lệnh chính

| Lệnh | Chức năng |
|---|---|
| `/help` | Hướng dẫn |
| `/zalo` | Đăng nhập hoặc xem trạng thái Zalo |
| `/zalologout` | Xóa Zalo session |
| `/prompt` | Tạo prompt hình ảnh |
| `/gia` | Tìm giá sản phẩm |
| `/dich [ja>vi\|vi>ja] <nội dung>` | Dịch chat công việc Nhật-Việt |
| `/reset` | Reset conversation context |
| `/history` | Xem lịch sử gần nhất |
| `/memory` | Xem trí nhớ dài hạn |
| `/forget` | Xóa trí nhớ dài hạn |
| `/notes` | Xem ghi chú |
| `/model` | Xem hoặc đổi model 9Router |
| `/status` | Xem provider chain |
| `/thongke [Nd\|Ngiờ]` | Thống kê lượt gọi theo user/kênh và theo model (mặc định 7 ngày, chỉ admin) |
| `/agent <câu hỏi>` | Agent tự tra cứu nhiều bước để trả lời (thử nghiệm, chỉ Telegram, chỉ admin) |
| `/userouter9` | Thử lại 9Router provider |
| `/zoompair <jid> [tên]` | Cấp quyền 1 jid Zoom nói chuyện với bot |
| `/zoomxoa` | Gỡ pairing Zoom hiện tại |
| `/zoomstatus` | Xem jid Zoom đang được pair |
| `/nhom` | Xem danh sách nhóm Zalo đang theo dõi |
| `/themnhom <group_id> <alias>` | Thêm nhóm Zalo vào allowlist |
| `/xoanhom <group_id\|alias>` | Ngừng theo dõi 1 nhóm Zalo |
| `/tongket <alias> [24h\|7d\|homnay\|homqua]` | Tổng kết nhóm Zalo (AI) |
| `/dangnoi <alias>` | Xem nguyên văn thảo luận nhóm Zalo hôm nay |
| `/zalopair <id_zalo> [tên]` | Cấp quyền thành viên cho 1 tài khoản Zalo |
| `/zaloadmin <id_zalo> [tên]` | Cấp/nâng quyền admin (dùng được lệnh nhóm) |
| `/zalohaquyen <id_zalo>` | Hạ 1 admin về thành viên thường |
| `/zalokhoa <id_zalo>` | Khóa 1 tài khoản Zalo |
| `/zalomokhoa <id_zalo>` | Mở khóa 1 tài khoản Zalo |
| `/zaloxoa <id_zalo>` | Xóa pairing 1 tài khoản Zalo |
| `/zalodanhsach` | Xem danh sách tài khoản Zalo đã pair |

## AI Agent (thử nghiệm)

Khác mọi lệnh khác trong bảng trên (mỗi lệnh là 1 pipeline CỐ ĐỊNH - nhận
input, gọi đúng 1-2 hàm theo thứ tự lập trình sẵn, trả lời), `/agent <câu
hỏi>` (`ai/agent_service.py`) để MODEL tự quyết định:

- Có cần tra cứu gì không, và tra cứu MẤY LẦN (tối đa `MAX_AGENT_STEPS = 4`
  bước) trước khi trả lời — vd *"so sánh giá iPhone 15 và 15 Pro"* sẽ tự gọi
  tool `tim_gia` 2 lần rồi mới tổng hợp câu trả lời, không cần bạn tách
  thành 2 lệnh `/gia` riêng.
- Dùng Google GenAI function calling (`google.genai.types.FunctionDeclaration`)
  qua riêng `api1` → `api2` (KHÔNG qua router9/groq/openrouter, vì các proxy
  đó không đảm bảo tương thích định dạng `functionCall`/`functionResponse`
  của Gemini) — đây cũng là 2 provider quota thấp nhất trong provider-chain,
  nên `/agent` **tốn quota nhanh hơn** lệnh thường, dùng cân nhắc.
- Tool hiện có: `tim_gia` (tái dùng logic `/gia`), `xem_thong_ke` (tái dùng
  `/thongke`). Thêm tool mới: viết 1 async function trong `ai/agent_service.py`
  rồi khai báo vào dict `_TOOLS` - vòng lặp tự dùng được, không cần sửa gì khác.
- Hiện chỉ bật ở Telegram (chưa nối vào Zalo/Zoom qua
  `services/channel_command_service.py`) vì đây là tính năng thử nghiệm.

## MCP Server (điều khiển lananh từ Claude Desktop/Claude Code)

`mcp_server.py` phơi 1 vài khả năng ĐỌC (read-only, an toàn) của bot qua
[Model Context Protocol](https://modelcontextprotocol.io) — để Claude
Desktop, Claude Code, hoặc bất kỳ MCP client nào gọi thẳng vào `lananh` mà
không cần mở Telegram/Zalo/Zoom. Chạy ở máy cá nhân, dùng chung
`DATABASE_URL`/API key với bot thật (đọc từ `.env` như bình thường) để số
liệu luôn khớp.

Tool hiện có: `tim_gia`, `xem_thong_ke`, `xem_trang_thai_provider` — CỐ Ý
không có tool ghi/phá huỷ nào (không đổi provider, không xoá trí nhớ...).

Cài đặt:
```bash
pip install -r requirements.txt -r requirements-mcp.txt
```

Cấu hình trong Claude Desktop/Claude Code (đường dẫn tới `mcp_server.py`
trong repo bạn):
```json
{
  "mcpServers": {
    "lananh": {
      "command": "python",
      "args": ["/đường/dẫn/tới/lananh/mcp_server.py"]
    }
  }
}
```

## Trang quản trị (Admin dashboard)

`web.py` phục vụ 1 trang HTML tại `/admin` (đăng nhập bằng `ADMIN_USER`/`ADMIN_PASS`,
phiên đăng nhập ký bằng HMAC dựa trên `ADMIN_PASS` — đổi `ADMIN_PASS` sẽ tự
vô hiệu mọi phiên đang mở). Không đặt 2 biến này thì `/admin` luôn từ chối
truy cập. Trang cho phép:

- Bật/tắt 9Router, Tavily, Agnes AI (tạo ảnh) và trí nhớ dài hạn theo từng user.
- Reset cooldown provider, đổi `PROVIDER_ORDER`.
- Xem thống kê **lượt gọi theo user/kênh** và **theo model** (`/admin/api/usage`,
  `/admin/api/usage/models`) — cùng số liệu với lệnh `/thongke` ở trên, chỉ
  khác là xem trên web thay vì chat.

## Giữ Render Free không bị ngủ (UptimeRobot)

Render Free tier tự **sleep sau ~15 phút không có request nào tới** — lần
request tiếp theo (vd Telegram gửi webhook) phải đợi service "cold start"
lại, có thể mất vài chục giây tới hơn 1 phút, dẫn tới bot phản hồi trễ hoặc
Telegram coi webhook timeout. Endpoint `/` (health check, không cần auth,
xem `web.py::health`) luôn trả `{"status": "ok"}` với `200`, dùng để ping giữ
service thức:

1. Tạo tài khoản miễn phí tại [UptimeRobot](https://dashboard.uptimerobot.com/login?rt=true).
2. Vào **Add New Monitor**:
   - **Monitor Type**: `HTTP(s)`
   - **Friendly Name**: `lananh` (hoặc tên bất kỳ)
   - **URL**: `https://<tên-service-render>.onrender.com/` (dùng đúng
     `WEBHOOK_BASE_URL` đã cấu hình, có dấu `/` ở cuối)
   - **Monitoring Interval**: `5 minutes` (gói free hỗ trợ tối thiểu 5 phút -
     đủ ngắn để luôn ping trước khi Render kịp sleep sau 15 phút)
3. Lưu monitor. UptimeRobot sẽ tự động gửi GET tới `/` mỗi 5 phút, giữ
   service luôn ở trạng thái "awake".
4. (Tuỳ chọn) Bật thông báo qua email/Telegram/Slack trong UptimeRobot để
   được báo ngay nếu service down thật (không phải do sleep).

Lưu ý: cách này chỉ giữ service khỏi sleep, **không** thay thế việc theo dõi
lỗi thật — vẫn nên kiểm tra log Render định kỳ. Nếu trước đây có dùng
GitHub Actions keep-alive workflow riêng, có thể tắt hẳn vì UptimeRobot làm
đúng việc này mà không cần thêm secret/CI trong repository.

## Kiểm tra chất lượng

```bash
pip install -r requirements-dev.txt
ruff check .
ruff format --check .
python -m compileall -q .
pytest -q

cd zalo-gateway
npm install
npm run check
```

CI chạy các kiểm tra Python và TypeScript trên push và pull request.

## Cấu trúc repository

```text
ai/                 Provider routing (9Router + Groq + OpenRouter + AI Studio)
channels/           Channel contracts, Zalo và Zoom persistence
core/               Config, encryption và database
handlers/           Telegram handlers
services/           Chat, commands, memory và telemetry dùng chung
stock/              Market data, validation, policy và backtest
zalo-gateway/        Node.js Zalo listener
web.py               FastAPI webhook entrypoint (Telegram + Zoom)
main.py              Local long-polling entrypoint
bot_app.py           Telegram app factory và lifecycle
mcp_server.py        MCP server (Claude Desktop/Claude Code) - xem mục "MCP Server"
```

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
`/forget`, `/notes`, `/model`, `/status`, `/thongke`, `/userouter9`) hoạt động GIỐNG HỆT
trên Zalo và Zoom vì cả hai đều đi qua chung `services/channel_chat_service.py`.

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
trực tiếp từ payload bridge), bot tự suy ra bằng cách lấy account_id có nhiều
nhóm đang theo dõi nhất (`channels/zalo_repository.py::resolve_default_account_id`)
— phù hợp
với thiết kế 1-chủ (chỉ 1 tài khoản Zalo BOT/B, dù nhiều người DÙNG có thể pair
để nhắn cho bot đó — xem mục "Zalo — nhiều tài khoản & phân quyền"). Nếu deploy
NHIỀU tài khoản Zalo BOT cùng lúc (nhiều `zalo-gateway` riêng biệt), hàm này
chỉ chọn được 1 tài khoản bot; cần chỉnh lại nếu muốn hỗ trợ multi-bot.

Zoom/Telegram **không thể tự thu thập tin nhắn nhóm Zalo** (đó vẫn là việc
của `zalo-gateway`) — chúng chỉ đọc lại dữ liệu Zalo đã có sẵn. Bản thân kênh
Zoom cũng không đọc thụ động được tin nhắn trong Zoom channel (chỉ nhận tin
gửi trực tiếp/@mention cho bot), nên KHÔNG có `/tongket`/`/dangnoi` tương
đương CHO NHÓM ZOOM — chỉ dùng được để xem lại nhóm ZALO.

## Bảo mật vận hành

- Không commit `.env`, token 9Router, Zalo session, Zoom secret, QR hoặc media.
- Không log credential hoặc signed Zalo CDN URL đầy đủ.
- Không public port `9901`.
- Dùng secret riêng cho webhook, diagnostics và Zalo bridge.
- Giữ `SETTINGS_ENC_KEY` ổn định và backup an toàn.
- Chỉ chạy một Zalo listener cho tài khoản bot.
- Rotate session ngay nếu nghi ngờ bị lộ.

## Giới hạn đã biết

- `gemini-webapi`, `zca-js`, DNSE và `vnstock` là dependency không chính thức hoặc không có SLA.
- Stock module là research assistant, không phải broker hoặc tư vấn viên được cấp phép.
- Backtest phụ thuộc độ phủ provider và không bảo đảm hiệu suất tương lai.
- Dự án cố ý chỉ hỗ trợ một người dùng; không có tenant isolation, billing, roles hoặc horizontal scaling.
- Render Free tự sleep sau ~15 phút không có request — xem mục "Giữ Render Free không bị ngủ (UptimeRobot)" ở trên để giữ service luôn thức.
- `/agent` và `mcp_server.py` là tính năng mới, mới kiểm tra bằng biên dịch cú pháp (`py_compile`), CHƯA chạy thử end-to-end với API key/DB thật — test kỹ trước khi dùng cho việc quan trọng, đặc biệt vòng lặp function-calling trong `ai/agent_service.py`.
- Cảnh báo "toàn bộ provider down" (`services/monitor_service.py`) chỉ phát hiện được tình huống router9 chết/tắt CỘNG mọi provider cooldown khác đều exhausted cùng lúc — không phát hiện được lỗi khác (vd DB mất kết nối, code lỗi logic) không liên quan tới provider AI.

## Tài liệu bổ sung

- `docs/zalo-render.md`
- `zalo-gateway/README.md`
- `.env.example`
- `render.yaml`

## Trách nhiệm

Dùng cho mục đích cá nhân/nội bộ. Người vận hành chịu trách nhiệm về điều khoản của Google, Telegram, Zalo, nguồn dữ liệu thị trường và mọi quyết định đầu tư.
