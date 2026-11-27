# Lananh — Trợ lý AI cá nhân (Telegram · Zalo · Zoom)

Bản sửa Render 512 MB: [mã sửa, migration và hướng dẫn cập nhật hai service](docs/render512-fixes.md).
Đợt nâng cấp 26/10/2026 (migration 017-025, biến mới, việc cần làm khi deploy):
[docs/nang-cap-2026-10-26.md](docs/nang-cap-2026-10-26.md).

Trợ lý cho một chủ sở hữu (Zalo có thể thêm thành viên), chạy trên provider chain
9Router → Groq → OpenRouter → Google AI Studio (Gemini API 1 → API 2). Hoạt động trên
Telegram và tùy chọn Zalo/Zoom, dùng chung provider chain, trí nhớ dài hạn, công cụ,
nhắc việc, chi tiêu, bản tin sáng và phân tích cổ phiếu Việt Nam.

## Trạng thái MVP

Repository được thiết kế cho **một chủ sở hữu**:

- Telegram chỉ chấp nhận `ALLOWED_USER_ID`.
- Zalo hỗ trợ **nhiều tài khoản** cùng nhắn cho 1 bot, phân quyền admin/thành viên,
  **mỗi tài khoản 1 bộ nhớ/ngữ cảnh chat riêng** (không lộ bí mật giữa các bên),
  kể cả tài khoản Zalo của chính chủ bot. Người lạ chưa pair: bot im lặng. Bot
  không bao giờ trả lời trong nhóm Zalo (chỉ đọc nhóm trong allowlist).
- Zoom Team Chat ghép đôi (pairing) đúng 1 jid được phép nói chuyện với bot.
- Telegram, Zalo và Zoom dùng chung bộ máy trí nhớ, tools và stock pipeline; dữ
  liệu tách theo người: Telegram + Zoom + web là chủ bot, mỗi tài khoản Zalo một
  ngữ cảnh riêng (trí nhớ, lịch sử, ghi chú, nhắc việc, chi tiêu, danh mục).
- Session/token nhạy cảm được mã hóa trước khi lưu database.
- Webhook và background tasks được drain khi shutdown.
- Backtest nặng bị chặn trên Render Web Service.
- Python và TypeScript có lint, format, test và CI (`.github/workflows/ci.yml`);
  Render chỉ deploy khi mọi check của commit xanh (`autoDeployTrigger: checksPass`).
- Mỗi người dùng có hàng đợi riêng; tổng lượt AI song song bị chặn bởi
  `MAX_CONCURRENT_TURNS` (mặc định 2) để vừa RAM 512 MB, trong đó
  `OWNER_RESERVED_SLOTS` (mặc định 1) chỗ luôn dành cho chủ bot (Telegram/Zoom/web).
- Mỗi lượt trả lời có hạn chót tổng `CHAT_TURN_TIMEOUT_SEC` (mặc định 200s, luôn
  dưới 240s gateway Zalo chờ) - MỘT hạn chót cho cả nhánh cổ phiếu lẫn chat, mọi
  lời gọi AI (kể cả cập nhật trí nhớ nền, tạo ảnh, /ragxuly) đi qua
  `ai.timeouts.bounded()`; sự kiện webhook lỗi thử lại tối đa
  `WEBHOOK_MAX_ATTEMPTS` lần rồi chuyển DEAD và báo chủ bot 1 lần (lỗi chắc chắn
  lặp như hết hạn chót, Telegram BadRequest thì kết thúc ngay ở lần đầu).
- Prompt gửi provider có ngân sách (`PROMPT_BUDGET_CHARS`, mặc định 40k ký tự):
  cắt lịch sử cũ → grounding → trí nhớ, không bao giờ cắt câu hỏi/persona
  (`ai/prompt_budget.py`).
- Zalo và Zoom đi chung 1 dispatcher (`services/channel_dispatch.py`): cache
  idempotency → `/fb_*` ngoài khoá lượt → lệnh chỉ đọc ngoài khoá lượt → trần
  lượt của thành viên Zalo → khoá lượt → lệnh nhóm → chat (huỷ được bằng `/huy`).
- Supabase chập chờn không làm mất câu trả lời: ghi DB sau khi trả lời là
  best-effort, đọc trí nhớ/lịch sử lỗi thì trả lời không kèm, circuit breaker
  báo lỗi nhanh khi DB mất hẳn (xem "Supabase chập chờn" bên dưới).
- Gateway Zalo tự kết nối lại khi listener đóng (backoff 5s → 5 phút, tối đa 10
  lần), thử gửi lại tin 3 lần, và báo trạng thái về Python
  (`POST /internal/zalo/gateway-state`) để chủ bot nhận cảnh báo khi mất kết nối,
  kết nối lại hoặc cần đăng nhập lại.

Đây không phải hệ thống multi-tenant, nền tảng tư vấn tài chính được cấp phép hoặc broker đặt lệnh.

## Tính năng

Chi tiết từng phần nằm trong `docs/` (chuyển từ README này ra cho dễ đọc):

- **Trợ lý chung và Telegram** - [docs/tro-ly.md](docs/tro-ly.md): trò chuyện, trí nhớ, tra web
  (Tavily rồi web search của provider), bảo vệ quota, chống prompt injection, nhắc việc, ngày nhớ,
  chi tiêu, dữ liệu đời sống VN, `/bantin`, `/chuy`, giờ yên lặng, việc nền `/viec`, tin thoại,
  ảnh, tài liệu, Kỳ Môn; hỏi tự nhiên thay cho lệnh; danh sách lệnh chính; `/agent`; bản tin sáng.
- **Zalo, Zoom, Zalo → Facebook Page** - [docs/zalo.md](docs/zalo.md): thiết lập Zalo, nhiều tài
  khoản và phân quyền, `/tongket`, tín hiệu cổ phiếu `/tinhieu`, đăng bài từ nhóm Zalo lên Page
  (lọc bài, lọc trùng giữa các nhóm, tên nhóm), Page chứng khoán, Zoom Team Chat.
- **Phân tích cổ phiếu Việt Nam** - [docs/co-phieu.md](docs/co-phieu.md).
- **Web LCTA, app Android, trang `/admin`** - [docs/web.md](docs/web.md).
- **Cấu hình, chạy local, deploy Render** - [docs/cau-hinh-deploy.md](docs/cau-hinh-deploy.md).
- **Giám sát, sao lưu, truy vết, bảo mật vận hành** - [docs/van-hanh.md](docs/van-hanh.md).

## Kiến trúc

```text
Telegram webhook ─┐
Zoom webhook ─────┼─ FastAPI / shared services ─ provider chain (9Router → Groq →
Zalo gateway ─────┘              │                OpenRouter → Gemini API 1/2)
(/internal/zalo/*)               ├─ memory, tools, nhắc việc, chi tiêu
                                 ├─ stock research + danh mục
                                 ├─ job nền: scheduler, bản tin, cảnh báo giá,
                                 │  hỏi thăm, watchdog, gom lỗi
                                 └─ Supabase Postgres

Render Docker service (supervisord)
├── Uvicorn (1 worker): webhook, bridge, scheduler, web LCTA, /admin và assistant
└── Node.js: Zalo listener và loopback control server

GitHub Actions: CI (chặn deploy khi đỏ), Dependency audit, Backup DB, Evals (chạy tay)
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

## Bắt đầu nhanh

1. Chép `.env.example` thành `.env`, điền các biến bắt buộc (xem
   [docs/cau-hinh-deploy.md](docs/cau-hinh-deploy.md#cấu-hình-bắt-buộc)).
2. Chạy local hoặc deploy Render theo [docs/cau-hinh-deploy.md](docs/cau-hinh-deploy.md).
3. Bật Zalo/Zoom (tuỳ chọn) theo [docs/zalo.md](docs/zalo.md).

## Kiểm tra chất lượng

```bash
pip install -r requirements-dev.txt && pip check
ruff check .                         # gồm bugbear (B) và ASYNC
ruff format --check <file đã sửa>   # repo cũ chưa format toàn bộ
pyright                              # pyrightconfig.json, mức basic (CI fail khi có error)
pip-audit -r requirements.txt --strict
python -m compileall -q .
pytest -q                            # test/conftest.py chặn mạng thật; @pytest.mark.network để mở
python -m core.migrations            # migrations.lock khớp file migration

# Test SQL chạy trên Postgres nhúng (PGlite, cần Node 18+):
(cd test/sql_support && npm ci)
TEST_PGLITE=1 pytest -q test/test_delivery_sql.py test/test_migrations.py test/test_zalo_outbox_sql.py test/test_ops_sql.py test/test_observability_sql.py

cd zalo-gateway
npm install
npm run check
npm run format:check
npm test
```

CI (`.github/workflows/ci.yml`) chạy trên push và pull request: `ruff check .`,
`ruff format --check` cho file `.py` đã đổi, `pytest -q` (gồm `test_migration_lock.py`
và `test_env_example.py`), `pyright`, build Docker image rồi chạy thử trong image
(import, `pip check`, node, zca-js), test SQL trên PGlite (đủ 5 file) và
kiểm tra/format/test của `zalo-gateway`. `pip-audit` + `npm audit` nằm riêng ở
`.github/workflows/audit.yml` (PR + hằng tuần) để không chặn deploy. Dependabot
(`.github/dependabot.yml`) mở PR cập nhật pip/npm/actions/docker, chỉ đề xuất bản đã
phát hành ít nhất 7 ngày.

### Golden set đánh giá câu trả lời

- `test/evals/*.yaml` có 41 case tiếng Việt (persona, trí nhớ, hỏi thăm, router tool,
  grounding, prompt injection, cổ phiếu, ngôn ngữ); định dạng case ghi ở
  `test/evals/harness.py`.
- Tier A chạy trong `pytest -q` mặc định, không gọi mạng
  (`test/test_evals_offline.py`): kiểm định dạng case, bộ kiểm tất định trên câu
  mẫu tốt/xấu, tên tool router và giờ nhắc do code tính, rào prompt injection, bảng
  câu cho bộ lọc router.
- Tier B gọi provider thật, chỉ chạy khi chủ động bật:
  - local: `RUN_LIVE_EVALS=1 GROQ_API_KEY=... python scripts/run_evals.py --max-calls 80 [--judge] [--only persona,router] --out eval-report.json`
    (thoát mã 1 khi bộ kiểm tất định trượt), hoặc
    `RUN_LIVE_EVALS=1 pytest -q -m live_llm test/test_evals_live.py`;
  - GitHub: Actions → "Evals (golden set)" → Run workflow (chỉ chạy tay), cần secret
    `EVAL_GROQ_API_KEY` / `EVAL_OPENROUTER_API_KEY` / `EVAL_GOOGLE_AI_STUDIO_API_KEY`,
    nên là key riêng để không đốt quota của bot.
- Đánh giá dùng DB giả trong RAM, không đụng DB production; từ chối chạy trên Render.
- Bộ kiểm tất định là cổng chặn; giám khảo LLM chỉ để tham khảo, ghim bằng
  `EVAL_JUDGE_PROVIDER`/`EVAL_JUDGE_MODEL`.
- Repo public nên báo cáo trên Actions ai đăng nhập GitHub cũng xem được (chỉ chứa
  case tổng hợp).
- `/xuateval [Nd]` trên Telegram xuất các câu chủ bot bấm 👎 thành file YAML case
  ứng viên. File chứa câu hỏi thật: phải bỏ thông tin cá nhân và thêm check trước
  khi chép vào `test/evals/`.

Phiên Claude Code cloud: `.claude/hooks/session-start.sh` (SessionStart hook) tự
tạo `.venv`, cài `requirements-dev.txt` và `npm install` cho gateway + PGlite.

## Cấu trúc repository

```text
ai/                 Provider routing (9Router + Groq + OpenRouter + AI Studio), cooldown
                    429 (cooldown.py), ghi lượt gọi (call_log.py), agent (agent_service.py),
                    hạn chót lượt (timeouts.py), bản nháp stream (drafts.py)
channels/           Channel contracts, Zalo và Zoom persistence; router.py (bridge
                    /internal/zalo/message), zalo_media.py (voice/file/notice),
                    zalo_onboarding.py (lời chào), zalo_text.py (styles zca-js)
core/               Config, encryption, database (+ repositories/ chứa SQL), migrations
                    (+ migrations.lock), untrusted.py (rào dữ liệu ngoài), urls.py
                    (lọc link lạ), trace.py (mã lượt), address.py (xưng hô), lunar_math.py
handlers/           Telegram handlers mỏng (parse lệnh -> gọi service -> trả lời):
                    commands.py (chung), provider_commands.py (model/router9/tavily/anh/loi),
                    pairing_commands.py (zoom*/zalo*), zalo_bridge_commands.py (nhóm, fb_*),
                    chat_router.py, stock_handler.py, portfolio_commands.py,
                    media_handler.py, voice_handler.py, document_handler.py,
                    daily_data_commands.py, reminder_commands.py, expense_commands.py,
                    feedback_commands.py, callbacks.py (nút inline), turn_feedback.py,
                    unsupported.py (vị trí, tin chưa hỗ trợ)
services/           Logic dùng chung cho mọi kênh: channel_chat_service.run_chat_turn
                    (pipeline chat), channel_dispatch (Zalo/Zoom), memory_service,
                    tools + tool_registry, reminder_service, expense_service,
                    morning_brief, attention (/chuy), notify_hub (gom thông báo,
                    giờ yên lặng), background_jobs (/viec), routines (/lichtacvu),
                    vn_daily_chat, kymon, voice/vision/document_service,
                    portfolio_* / price_alerts, usage_quota, watchdog, error_digest,
                    quota_burn, price_search (/gia), usage_stats (/thongke)...
stock/              Market data, validation, policy, lịch giao dịch, danh mục và backtest
assets/             Ý nghĩa soạn sẵn: tu_vi.yaml, than_so_hoc.yaml, ky_mon.yaml; tts_viet_tat.yaml (đọc giọng /noi)
rag/                Kho kiến thức .md cho lệnh /rag (mỗi heading 1 mẩu kiến thức)
migrations/         NNN_*.sql (001-030), checksum ghim trong migrations.lock
scripts/            backup_drill.py, run_evals.py, train_trend_model.py, run-zalo-gateway.sh,
                    benchmark_memory.py (đo RSS theo kịch bản tải, offline)
test/evals/         Golden set đánh giá (YAML + harness.py)
zalo-gateway/        Node.js Zalo listener
web.py               FastAPI webhook entrypoint (Telegram + Zoom), /healthz
web_site.py          Web LCTA (lịch, thời tiết, tử vi, thần số học, Kỳ Môn) + /api/v1
web_app_api.py       API app Android: /api/v1/app/pair, /api/v1/app/dashboard
android/             App Android màn hình thông tin (Kotlin, không thư viện ngoài)
web_admin.py         Trang /admin và admin API (đăng nhập có giới hạn số lần sai);
                     /admin/api/memory-usage: RAM container + từng tiến trình;
                     /admin/api/db-usage: dung lượng bảng ảnh và job VACUUM FULL;
                     /admin/api/watchdog, /admin/api/usage/*, /admin/api/quota
main.py              Local long-polling entrypoint
bot_app.py           Telegram app factory và lifecycle
```

## Giới hạn đã biết

- `zca-js`, DNSE, `vnstock`, VCI (lịch sự kiện quyền `/v1/events`) và các nguồn đời sống (SJC, Vietcombank, xskt.com.vn) là nguồn không chính thức hoặc không có SLA; parser lỗi thì báo lỗi/quay về tra web, không ghi dữ liệu sai.
- Khối ngoại nhiều phiên (lịch sử mua/bán ròng theo chuỗi ngày) hiện KHÔNG có trong bot: `vnstock` chưa có provider nào implement thật endpoint này (chỉ là stub rỗng). Chỉ có khối ngoại phiên gần nhất (qua `price_board()`).
- 1 lượt `/phantich` vẫn gọi tối đa 5 lệnh LLM tuần tự (news → bull → bear → manager → tổng hợp), có nghỉ `ROUTER9_STEP_DELAY_SEC` (mặc định 3s) giữa mỗi lệnh để giãn tải gateway 9Router. Bù lại bản số liệu rule-based giờ được gửi NGAY trước khi debate chạy (báo cáo AI gửi tiếp thành tin nhắn thứ hai), nên không phải chờ tới hơn 2 phút mới thấy tin đầu tiên; AI lỗi hoặc quá hạn chót thì có 1 câu báo kèm tên mã thay vì im lặng.
- Stock module là research assistant, không phải broker hoặc tư vấn viên được cấp phép.
- Backtest phụ thuộc độ phủ provider và không bảo đảm hiệu suất tương lai.
- Dự án cố ý chỉ hỗ trợ một người dùng; không có tenant isolation, billing, roles hoặc horizontal scaling.
- Render Free tự sleep sau ~15 phút không có request — xem mục "Giữ Render Free không bị ngủ (UptimeRobot)" trong [docs/cau-hinh-deploy.md](docs/cau-hinh-deploy.md) để giữ service luôn thức.
- `/agent` CHƯA chạy thử end-to-end với API key/DB thật — test kỹ trước khi dùng cho việc quan trọng, đặc biệt vòng lặp function-calling trong `ai/agent_service.py`. Để giảm prompt injection, `doc_link` chỉ đọc được URL có sẵn nguyên văn trong câu hỏi hoặc trong kết quả tool tìm kiếm của lượt đó. `/agent`, `/rag`, `/gia`, `*_chitiet` chưa huỷ được bằng `/huy`.
- Cảnh báo "toàn bộ provider down" (`services/monitor_service.py`) chỉ phát hiện được tình huống router9 chết/tắt CỘNG mọi provider cooldown khác đều exhausted cùng lúc. Lỗi khác (RAM, DB, inbox ứ đọng, job quá hạn, lỗi code) do watchdog và bộ gom lỗi báo; khi chính bot chết thì chỉ giám sát bên ngoài (UptimeRobot `/healthz`) báo được.
- Chép lời tin thoại, phân loại caption ảnh và nhận diện chi tiêu dựa trên từ khoá/heuristic: Whisper có thể nghe nhầm số (lượt thoại vì vậy chỉ đọc), "trà sữa 30k" bị ghi thành khoản chi (huỷ bằng "huỷ" trong 10 phút).
- Gateway Zalo chỉ gửi "đang soạn tin"/"đã xem" cho người đã được Python trả lời từ lúc gateway khởi động; dạng payload `chat.voice`, `share.file` lấy theo mã nguồn zca-js 2.1.2, chưa thấy trong lưu lượng thật.

## Tài liệu bổ sung

- `docs/tro-ly.md`, `docs/zalo.md`, `docs/co-phieu.md`, `docs/web.md`, `docs/cau-hinh-deploy.md`,
  `docs/van-hanh.md` — các phần chi tiết tách từ README (xem mục Tính năng ở trên)
- `docs/ke-hoach-nang-cap-2026-11.md` — kế hoạch tối ưu và nâng cấp 11/2026 và trạng thái từng việc

- `docs/avatar.md` — trang `/admin/avatar`: Lan Anh "người iu" qua ảnh chân dung sống có giọng nói (cách dùng, yêu cầu ảnh, biến `AVATAR_*`, giới hạn, đóng gói lại MediaPipe)
- `docs/nang-cap-jarvis.md` — các tính năng lấy ý từ các dự án Jarvis: `/chuy`, gom thông báo + giờ yên lặng (migration 026), việc nền `/viec` + tác vụ định kỳ `/lichtacvu` (migration 027), che bí mật trước khi gửi provider, duyệt cách trả lời, mô tả tool gọn, trả lời bằng giọng nói `/noi`, trang Nhật ký trong `/admin`, ghi ăn uống `/an` (migration 028), Gemini Live (chat dự phòng cuối + bộ đọc `/noi`), sửa sau chạy thật 27/10 (RAM: đường giá không nạp pandas/vnstock, `malloc_trim`, trần chỉ mục rag; Zoom link nghe; chuẩn hoá chữ + phong cách giọng `/noi`; hỏi giá sản phẩm không bị chặn)
- `docs/nang-cap-2026-10-26.md` — đợt nâng cấp 26/10/2026: thay đổi theo nhóm, danh sách việc khi deploy (migration 017-025, biến Render, GitHub secrets), việc chủ bot còn phải quyết, phần cố ý chưa làm, rủi ro
- `docs/backup-restore.md` — sao lưu/khôi phục DB, role chỉ-đọc, diễn tập, xoay khoá
- `docs/upgrade-2026-10.md` — đợt nâng cấp toàn diện 10/2026 (lỗi đã sửa, migration 015, biến mới, kết quả backtest offline, việc cần làm khi deploy)
- `docs/review-2026-10.md` — đợt sửa lỗi/bảo mật/tái cấu trúc tháng 10/2026 và việc cần làm khi deploy

- `docs/zalo-render.md`
- `zalo-gateway/README.md`
- `.env.example`
- `render.yaml`

## Trách nhiệm

Dùng cho mục đích cá nhân/nội bộ. Người vận hành chịu trách nhiệm về điều khoản của Google, Telegram, Zalo, nguồn dữ liệu thị trường và mọi quyết định đầu tư.
