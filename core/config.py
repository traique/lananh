import logging
import os
from pathlib import Path

import jinja2
import yaml
from dotenv import load_dotenv

from core import address

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()


def _parse_allowed_user_id(raw: str) -> int:
    """Parse ALLOWED_USER_ID thành int.

    Không dùng str.isdigit() vì hai lý do: ID âm (chat/group ID của Telegram
    có dạng -100...) bị coi là không hợp lệ nên bot báo "thiếu biến môi
    trường" thay vì báo giá trị sai; và isdigit() trả True cho ký tự số
    unicode như "²" trong khi int() lại ném ValueError, làm crash ngay lúc
    import module thay vì báo lỗi cấu hình tử tế.
    """
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "ALLOWED_USER_ID không phải số nguyên hợp lệ (nhận %r) — coi như chưa cấu hình.",
            raw,
        )
        return 0


_allowed_id_raw = os.getenv("ALLOWED_USER_ID", "0").strip()
ALLOWED_USER_ID = _parse_allowed_user_id(_allowed_id_raw)

# 9Router — gateway OpenAI-compatible dùng làm provider ĐẦU TIÊN của provider-chain
# (router9 -> api1 -> api2), thay cho cookie tài khoản Gemini cá nhân trước đây.
# Không rotate/hết hạn như cookie, chỉ cần 1 API key tĩnh.
ROUTER9_API_KEY = os.getenv("ROUTER9_API_KEY", "").strip()
ROUTER9_BASE_URL = os.getenv("ROUTER9_BASE_URL", "https://ninerouter-v2.onrender.com").strip()
ROUTER9_MODEL = os.getenv("ROUTER9_MODEL", "gpt-4o-mini").strip()

# 2 API key cho provider-chain (router9 -> api1 -> api2). GOOGLE_AI_STUDIO_API_KEY
# (tên biến cũ) vẫn được đọc để tương thích ngược, coi như alias của _1 nếu
# GOOGLE_AI_STUDIO_API_KEY_1 chưa được set riêng.
GOOGLE_AI_STUDIO_API_KEY_1 = (
    os.getenv("GOOGLE_AI_STUDIO_API_KEY_1", "").strip()
    or os.getenv("GOOGLE_AI_STUDIO_API_KEY", "").strip()
    or None
)
GOOGLE_AI_STUDIO_API_KEY_2 = os.getenv("GOOGLE_AI_STUDIO_API_KEY_2", "").strip() or None
# Alias giữ tương thích ngược cho code/tài liệu cũ còn tham chiếu tên này.
GOOGLE_AI_STUDIO_API_KEY = GOOGLE_AI_STUDIO_API_KEY_1
GOOGLE_AI_STUDIO_MODEL = os.getenv("GOOGLE_AI_STUDIO_MODEL", "gemini-3.5-flash-lite").strip()

HAS_ANY_AI_STUDIO_KEY = bool(GOOGLE_AI_STUDIO_API_KEY_1 or GOOGLE_AI_STUDIO_API_KEY_2)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


# Timeout cho 1 lượt gọi 9Router (giữ tên biến ROUTER9_CALL_TIMEOUT_SEC; alias
# GEMINI_COOKIE_CALL_TIMEOUT_SEC cũ vẫn được đọc nếu ROUTER9_CALL_TIMEOUT_SEC
# chưa set riêng, để không phá cấu hình Render đã lưu sẵn từ trước).
# 120s (trước 45s): client luôn xin stream (xem openai_compatible.
# post_chat_completion) nên byte chảy liên tục, không còn bị proxy trung gian
# cắt 504; timeout này là chặn TỔNG thời gian 1 lượt gọi. 120s đủ rộng cho
# bước tổng hợp cuối /phantich nhưng vẫn giới hạn thời gian chờ khi 9Router
# gặp sự cố để provider-chain còn kịp fallback.
ROUTER9_CALL_TIMEOUT_SEC = _env_int(
    "ROUTER9_CALL_TIMEOUT_SEC",
    _env_int("GEMINI_COOKIE_CALL_TIMEOUT_SEC", 120),
)
ROUTER9_MAX_CONCURRENCY = _env_int("ROUTER9_MAX_CONCURRENCY", 4)
# 9Router tự tra web: gửi web_search_options (chuẩn OpenAI Chat Completions)
# tới model ROUTER9_WEB_SEARCH_MODEL (trống = model đang dùng cho chat). TẮT mặc
# định: tài liệu 9Router không mô tả web search trong /chat/completions, và
# gateway từng âm thầm bỏ qua `tools`. Khi bật, câu trả lời chỉ được tin là đã
# tra web nếu kèm annotations url_citation - không có thì coi như chưa tra.
ROUTER9_WEB_SEARCH = _env_bool("ROUTER9_WEB_SEARCH", False)
ROUTER9_WEB_SEARCH_MODEL = os.getenv("ROUTER9_WEB_SEARCH_MODEL", "").strip()

# Khoảng nghỉ (giây) giữa các lệnh gọi LLM liên tiếp trong 1 lượt phân tích
# (news -> bull -> bear -> manager -> tổng hợp cuối, xem stock/debate.py và
# stock/analysis.py::analyze_symbol) - giãn tải cho gateway 9Router.
ROUTER9_STEP_DELAY_SEC = _env_int("ROUTER9_STEP_DELAY_SEC", 3)

# Groq — gateway OpenAI-compatible, provider thứ 2 của provider-chain.
# (router9 -> groq -> openrouter -> api1 -> api2). GROQ_REALTIME_MODEL là model
# dùng khi cần Groq TỰ tra web (tool browser_search chạy phía server Groq) -
# xem GROQ_WEB_SEARCH bên dưới; built-in web search có thể phát sinh phí theo
# Groq. Danh mục model free đổi theo thời gian - xem README mục Provider-chain
# nếu model mặc định ngừng hoạt động.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
# meta-llama/llama-4-scout-17b-16e-instruct (giá trị cũ) đã bị Groq khai tử
# (shutdown 17/07/2026, xem console.groq.com/docs/deprecations) - request sẽ
# lỗi 400/404 nếu còn dùng model đó. qwen/qwen3.6-27b hiện là model vision
# DUY NHẤT Groq liệt kê ở console.groq.com/docs/vision, miễn phí (free tier).
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b").strip()
# groq/compound và groq/compound-mini (giá trị mặc định cũ) đã bị Groq khai tử
# (shutdown 21/09/2026, console.groq.com/docs/deprecations) - mọi request lỗi.
# Thay bằng tool browser_search trên openai/gpt-oss-* (console.groq.com/docs/
# browser-search: hỗ trợ openai/gpt-oss-20b, gpt-oss-120b, gpt-oss-safeguard-20b).
# Render env còn lưu giá trị compound cũ thì tự đổi về mặc định mới (có log).
_GROQ_DEFAULT_REALTIME_MODEL = "openai/gpt-oss-120b"
_GROQ_DECOMMISSIONED_MODELS = {"groq/compound", "groq/compound-mini"}


def _normalize_groq_realtime_model(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return _GROQ_DEFAULT_REALTIME_MODEL
    if value.lower() in _GROQ_DECOMMISSIONED_MODELS:
        logger.warning(
            "GROQ_REALTIME_MODEL=%s đã bị Groq khai tử - dùng %s (tool browser_search).",
            value,
            _GROQ_DEFAULT_REALTIME_MODEL,
        )
        return _GROQ_DEFAULT_REALTIME_MODEL
    return value


GROQ_REALTIME_MODEL = _normalize_groq_realtime_model(os.getenv("GROQ_REALTIME_MODEL", ""))
# Groq tự tra web (browser_search) cho câu cần thông tin mới khi Tavily tắt/lỗi/
# quá mỏng, và cho nhánh tra cứu bắt buộc (giá vàng, tỷ giá...). Chỉ có hiệu
# lực khi GROQ_REALTIME_MODEL là model openai/gpt-oss-*.
GROQ_WEB_SEARCH = _env_bool("GROQ_WEB_SEARCH", True)
GROQ_CALL_TIMEOUT_SEC = _env_int("GROQ_CALL_TIMEOUT_SEC", 30)
# GROQ_CALL_TIMEOUT_SEC chỉ là timeout từng pha của httpx (connect/đọc từng
# chunk) - với response stream nhỏ giọt nó không bao giờ hết hạn. Biến dưới
# chặn TỔNG thời gian 1 lượt gọi (kể cả chờ semaphore) để provider-chain
# chắc chắn chuyển tiếp được.
GROQ_TOTAL_TIMEOUT_SEC = _env_int("GROQ_TOTAL_TIMEOUT_SEC", 90)
GROQ_MAX_CONCURRENCY = _env_int("GROQ_MAX_CONCURRENCY", 4)

# OpenRouter — gateway OpenAI-compatible miễn phí, provider thứ 3 của
# provider-chain. Model mặc định có hậu tố ":free" (không tốn phí) - danh mục
# model free trên OpenRouter đổi theo thời gian, xem README nếu model mặc
# định ngừng hoạt động.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free").strip()
# google/gemma-4-31b-it:free (giá trị cũ) không phải tên model có thật trên
# OpenRouter (không khớp bất kỳ slug nào trong openrouter.ai/collections/free-models
# - mọi request sẽ trả 400 "model not found"). google/gemma-4-26b-a4b-it:free
# là model Gemma 4 free hiện có, hỗ trợ vision (ảnh + video) trên OpenRouter.
OPENROUTER_VISION_MODEL = os.getenv(
    "OPENROUTER_VISION_MODEL", "google/gemma-4-26b-a4b-it:free"
).strip()
OPENROUTER_CALL_TIMEOUT_SEC = _env_int("OPENROUTER_CALL_TIMEOUT_SEC", 30)
# Chặn tổng thời gian 1 lượt gọi OpenRouter - xem chú thích GROQ_TOTAL_TIMEOUT_SEC.
OPENROUTER_TOTAL_TIMEOUT_SEC = _env_int("OPENROUTER_TOTAL_TIMEOUT_SEC", 90)
OPENROUTER_MAX_CONCURRENCY = _env_int("OPENROUTER_MAX_CONCURRENCY", 4)
# OpenRouter tự tra web qua plugin "web" (openrouter.ai/docs/guides/features/
# web-search). TẮT mặc định: tra web TỐN CREDIT kể cả với model ":free" (Exa
# ~$0.007/request) - tài khoản không nạp credit sẽ lỗi. Bật khi đã nạp credit.
OPENROUTER_WEB_SEARCH = _env_bool("OPENROUTER_WEB_SEARCH", False)

# Tavily — web search dùng làm grounding cho chat, bật/tắt qua lệnh
# /tavily on|off (ai/tavily_client.py: get_enabled()/set_enabled()), mặc định
# tắt. Không thuộc provider-chain (router9/groq/openrouter/api1/api2) - chỉ
# chèn kết quả search vào prompt trước khi gọi provider hiện hành.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
TAVILY_BASE_URL = os.getenv("TAVILY_BASE_URL", "https://api.tavily.com").strip()
TAVILY_CALL_TIMEOUT_SEC = _env_int("TAVILY_CALL_TIMEOUT_SEC", 20)

# Lọc link lạ trong câu trả lời chat (core/urls.py): link không có trong tin
# người dùng/dữ liệu tra cứu bị hiện dạng hxxps://ten[.]mien. "untrusted" (mặc
# định): chỉ lọc lượt có dữ liệu ngoài (Tavily, đọc trang...) trong prompt -
# đúng lượt có thể bị prompt injection; "all": lọc mọi lượt chat; "off": tắt.
# Lượt provider tự tra web (Google Search, browser_search) không lọc vì link
# nguồn do provider tìm ra, không nằm trong dữ liệu của bot.
REPLY_URL_FILTER = os.getenv("REPLY_URL_FILTER", "untrusted").strip().lower() or "untrusted"

# vnstock — API key MIỄN PHÍ (gói Community, đăng ký tại vnstocks.com/login)
# nâng rate limit 20 -> 60 request/phút và số kỳ BCTC 4 -> 8 kỳ so với chế độ
# khách. Không set thì vnstock vẫn chạy ở mức Guest như trước (xem
# stock/providers.py::ensure_vnstock_api_key()).
VNSTOCK_API_KEY = os.getenv("VNSTOCK_API_KEY", "").strip()

# Giới hạn số request đồng thời tới VCI/vnstock trên toàn app (xem
# stock/providers.py::get_vnstock_semaphore()) - tránh bắn hơn chục request
# song song cho 1 lượt phân tích khiến VCI rate-limit/chặn tạm thời, làm cả
# valuation lẫn khối ngoại đều rỗng cùng lúc.
VNSTOCK_MAX_CONCURRENCY = _env_int("VNSTOCK_MAX_CONCURRENCY", 3)

# Agnes AI (gateway bên thứ ba, tương thích OpenAI - xem ai/agnes_client.py) -
# dùng riêng cho lệnh /anh (tạo ảnh), KHÔNG thuộc provider-chain chat chính.
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "").strip()
AGNES_BASE_URL = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").strip()
AGNES_IMAGE_MODEL = os.getenv("AGNES_IMAGE_MODEL", "agnes-image-2.1-flash").strip()
AGNES_CALL_TIMEOUT_SEC = _env_int("AGNES_CALL_TIMEOUT_SEC", 90)
TAVILY_MAX_RESULTS = _env_int("TAVILY_MAX_RESULTS", 5)

# Web reader (đọc link/RSS bất kỳ cho /agent) - dùng Jina Reader (r.jina.ai,
# miễn phí, không cần key) để lấy text sạch từ 1 URL, và feedparser để đọc
# RSS/Atom. Xem services/web_reader.py.
WEB_READER_TIMEOUT_SEC = _env_int("WEB_READER_TIMEOUT_SEC", 20)
WEB_READER_MAX_CHARS = _env_int("WEB_READER_MAX_CHARS", 6000)
RSS_READER_MAX_ITEMS = _env_int("RSS_READER_MAX_ITEMS", 8)
# Trần số byte tải về cho 1 trang/feed (đọc dạng stream, dừng ngay khi vượt) -
# 1 link PDF/video vài chục MB không được phép nạp trọn vào RAM 512 MB.
WEB_READER_MAX_BYTES = max(64 * 1024, _env_int("WEB_READER_MAX_BYTES", 2 * 1024 * 1024))


# ─── Provider-chain (router9 -> groq -> openrouter -> api1 -> api2) + trí nhớ
# hội thoại ────────────────────────────────────────────────────────────────
# 9Router chết -> chuyển hẳn sang provider kế tiếp (không thử lại router9 mỗi
# tin, chỉ có background probe + /userouter9 + đổi env mới kích hoạt thử lại
# router9). Groq/OpenRouter/API hết quota (429) -> cooldown cố định rồi tự
# thử lại (xem ai/provider_state.py).
# Số LƯỢT hỏi-đáp gần nhất của phiên đưa vào prompt (mỗi lượt = 2 dòng lịch sử;
# trước đây là số DÒNG nên 8 chỉ còn ~4 lượt). prompt_budget vẫn cắt tin cũ nhất
# khi vượt ngân sách.
CHAT_HISTORY_TURNS = _env_int("CHAT_HISTORY_TURNS", 10)
CHAT_SESSION_TIMEOUT_SEC = _env_int("CHAT_SESSION_TIMEOUT_SEC", 21600)  # 6 giờ
ROUTER9_PROBE_INTERVAL_SEC = _env_int(
    "ROUTER9_PROBE_INTERVAL_SEC", _env_int("COOKIE_PROBE_INTERVAL_SEC", 900)
)  # 15 phút
API_QUOTA_COOLDOWN_SEC = _env_int("API_QUOTA_COOLDOWN_SEC", 3600)  # 60 phút
# Hạn chót TỔNG cho 1 lượt trả lời qua provider-chain (mọi provider + lượt thử
# lại cộng dồn). Phải nhỏ hơn 240s - thời gian zalo-gateway chờ /message trước
# khi bỏ cuộc - để người dùng nhận được thông báo lỗi rõ ràng thay vì im lặng
# và slot MAX_CONCURRENT_TURNS được trả lại.
CHAT_TURN_TIMEOUT_SEC = max(30, min(230, _env_int("CHAT_TURN_TIMEOUT_SEC", 200)))
# Cập nhật trí nhớ dài hạn chạy NỀN sau mỗi lượt chat (ngoài hạn chót lượt):
# vẫn phải có trần riêng để 1 lượt gọi Gemini treo không giữ task mãi.
MEMORY_UPDATE_TIMEOUT_SEC = max(15, min(180, _env_int("MEMORY_UPDATE_TIMEOUT_SEC", 90)))
# /ragxuly dọn file dài theo từng phần, chạy nền ngoài khoá lượt chat; trần
# TỔNG cho cả file (trước đây chỉ có 180s/phần, file 50 phần = 2,5 giờ).
RAG_CLEAN_TOTAL_TIMEOUT_SEC = max(120, min(3600, _env_int("RAG_CLEAN_TOTAL_TIMEOUT_SEC", 900)))

# Webhook inbox (core/webhook_inbox.py): số lần thử tối đa cho 1 sự kiện
# Telegram/Zoom trước khi chuyển DEAD (ngừng thử, báo chủ bot 1 lần), và số
# ngày giữ dòng DEAD để tra cứu trước khi xoá.
WEBHOOK_MAX_ATTEMPTS = max(1, _env_int("WEBHOOK_MAX_ATTEMPTS", 8))
WEBHOOK_DEAD_RETENTION_DAYS = max(1, _env_int("WEBHOOK_DEAD_RETENTION_DAYS", 14))

# services/monitor_service.py::_monitor_loop() - chu kỳ kiểm tra "TOÀN BỘ
# provider trong PROVIDER_ORDER cùng lúc không dùng được" (router9 chết/tắt
# VÀ mọi provider cooldown còn lại đều đang exhausted). Chỉ đọc state trong
# RAM (provider_state), không gọi API nào nên có thể để ngắn (mặc định 5
# phút) mà không tốn quota.
MONITOR_INTERVAL_SEC = _env_int("MONITOR_INTERVAL_SEC", 300)  # 5 phút

# Thứ tự ưu tiên thử provider, đọc từ env PROVIDER_ORDER (vd "api1,api2,router9"
# để dùng API chính thức làm xương sống - xem README mục Provider-chain để
# cân nhắc trước khi đổi). Mặc định: router9 -> groq -> openrouter -> api1 ->
# api2. Chi phí phụ thuộc provider/model/tool thực tế; Gemini official đứng cuối
# làm lưới an toàn.
_PROVIDER_ORDER_RAW = os.getenv("PROVIDER_ORDER", "router9,groq,openrouter,api1,api2").strip()


def _parse_provider_order(raw: str) -> list[str]:
    valid = {"router9", "groq", "openrouter", "api1", "api2"}
    order = [p.strip().lower() for p in raw.split(",") if p.strip()]
    # Tương thích ngược: cấu hình cũ còn ghi "cookie" (Render env đã lưu từ
    # trước khi đổi sang 9Router) -> coi như "router9".
    order = ["router9" if p == "cookie" else p for p in order]
    order = [p for p in order if p in valid]
    seen = set()
    deduped = []
    for p in order:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    # Đảm bảo đủ cả 5 provider (thêm provider bị thiếu vào cuối theo thứ tự
    # mặc định), để không vô tình loại hẳn 1 provider chỉ vì gõ thiếu trong env.
    for p in ("router9", "groq", "openrouter", "api1", "api2"):
        if p not in deduped:
            deduped.append(p)
    return deduped


PROVIDER_ORDER = _parse_provider_order(_PROVIDER_ORDER_RAW)

# ─── Scheduler: reminder + daily digest danh mục (Bước 6) ──────────────────
REMINDER_CHECK_INTERVAL_SEC = _env_int("REMINDER_CHECK_INTERVAL_SEC", 30)
ENABLE_DAILY_DIGEST = _env_bool("ENABLE_DAILY_DIGEST", True)
# Cảnh báo danh mục cuối phiên (chạm stop/target, breakout Donchian) - services/portfolio_alerts.py.
ENABLE_PORTFOLIO_ALERTS = _env_bool("ENABLE_PORTFOLIO_ALERTS", True)
DAILY_DIGEST_HOUR_VN = _env_int("DAILY_DIGEST_HOUR_VN", 8)
# Mặc định digest bỏ qua cuối tuần/ngày lễ (giá vẫn là phiên trước, gửi lại y bản cũ).
DIGEST_ON_NON_TRADING_DAYS = _env_bool("DIGEST_ON_NON_TRADING_DAYS", False)

# Bản tin buổi sáng tổng hợp từ RSS, gửi qua Zalo + Zoom cho chủ bot lúc
# MORNING_NEWS_HOUR_VN giờ VN - xem services/morning_news.py. Nguồn RSS mặc
# định gồm kinh doanh/chứng khoán + tin tổng hợp VnExpress/Tuổi Trẻ/GenK; đổi qua
# env MORNING_NEWS_RSS_FEEDS (phân cách bằng dấu phẩy) nếu muốn nguồn khác.
MORNING_NEWS_ENABLED = _env_bool("MORNING_NEWS_ENABLED", True)
MORNING_NEWS_HOUR_VN = _env_int("MORNING_NEWS_HOUR_VN", 8)
_MORNING_NEWS_RSS_FEEDS_RAW = os.getenv(
    "MORNING_NEWS_RSS_FEEDS",
    # Giữ nguyên 3 nguồn kinh tế/chứng khoán hiện tại, đồng thời bổ sung
    # các feed tin tổng hợp VnExpress/Tuổi Trẻ/GenK để bản tin sáng có thêm
    # thời sự và công nghệ nổi bật.
    "https://vnexpress.net/rss/kinh-doanh.rss,"
    "https://cafef.vn/thi-truong-chung-khoan.rss,"
    "https://vietstock.vn/830/chung-khoan/co-phieu.rss,"
    "https://vnexpress.net/rss/tin-moi-nhat.rss,"
    "https://tuoitre.vn/home.rss,"
    "https://genk.vn/rss/home.rss",
)
MORNING_NEWS_RSS_FEEDS = [
    url.strip() for url in _MORNING_NEWS_RSS_FEEDS_RAW.split(",") if url.strip()
]

# Bản tin sáng CÁ NHÂN (services/morning_brief.py): mỗi người tự bật bằng /bantin
# trên kênh của mình (mặc định không ai nhận). Thời tiết theo thành phố riêng, âm
# lịch, nhắc việc hôm nay, vàng/tỷ giá, danh mục (ngày giao dịch), tin (Telegram
# tắt mặc định - đúng lựa chọn không gửi tin RSS qua Telegram).
MORNING_BRIEF_ENABLED = _env_bool("MORNING_BRIEF_ENABLED", True)
MORNING_BRIEF_DEFAULT_HOUR = max(4, min(12, _env_int("MORNING_BRIEF_DEFAULT_HOUR", 7)))
# Trần số thành viên Zalo bật bản tin (RAM + vệ sinh chống khoá Zalo).
MORNING_BRIEF_MAX_MEMBERS = max(0, min(100, _env_int("MORNING_BRIEF_MAX_MEMBERS", 20)))
# Zalo controller / Zoom của chủ bot đã bật bản tin -> bản tin RSS 8h gộp vào
# bản tin cá nhân (1 tin thay vì 2) thay vì gửi riêng.
MORNING_BRIEF_ABSORB_NEWS = _env_bool("MORNING_BRIEF_ABSORB_NEWS", True)
# 1 câu mở đầu giọng Lan Anh do AI viết (chỉ chủ bot, tốn 1 lượt AI/ngày).
MORNING_BRIEF_AI_INTRO = _env_bool("MORNING_BRIEF_AI_INTRO", False)
# Dòng tổng chi tiêu tháng này của chính người nhận (chỉ hiện khi đã ghi khoản nào).
MORNING_BRIEF_EXPENSES = _env_bool("MORNING_BRIEF_EXPENSES", True)
# Tin ngắn kiểu "cafe 45k", "đổ xăng 1tr2" được ghi chi tiêu bằng code (không tốn AI);
# false = chỉ ghi khi nói rõ "ghi chi tiêu ..." (router AI) hoặc /chitieu.
EXPENSE_CHAT_CAPTURE = _env_bool("EXPENSE_CHAT_CAPTURE", True)

# Giờ đăng (giờ VN, dạng HH:MM, nhiều mốc cách nhau bằng dấu phẩy) của Page chứng
# khoán riêng - xem services/market_page.py. Giá trị sai định dạng thì dùng mặc định.
MARKET_NEWS_TIMES_VN = os.getenv("MARKET_NEWS_TIMES_VN", "08:30").strip()
MARKET_STOCK_TIMES_VN = os.getenv("MARKET_STOCK_TIMES_VN", "15:20").strip()

CHAT_SKILL_PATH = Path(os.getenv("CHAT_SKILL_PATH", "chat_skill.yaml").strip())
# File tham chiếu văn phong/thuật ngữ dịch Nhật-Việt cho lệnh /dich (tùy chọn,
# fail-open nếu thiếu - xem services/translate_service.py::_reference_guide).
TRANSLATE_REFERENCE_PATH = os.getenv("TRANSLATE_REFERENCE_PATH", "translate_reference.txt").strip()
# Giới hạn độ dài nội dung /dich (ký tự): đoạn quá dài dễ vượt timeout 9Router
# và chết giữa chừng - chặn trước với thông báo thân thiện thay vì lỗi mờ nhạt.
TRANSLATE_MAX_CHARS = _env_int("TRANSLATE_MAX_CHARS", 4000)
_CHAT_SKILL_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "templates" / "chat_skill_prompt.j2"
)

# Jinja Environment dựng 1 lần ở module level (thay vì mỗi lần render), giống
# cách stock_analysis.py compile template của nó. Environment tự cache template
# đã compile bên trong.
_CHAT_SKILL_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(_CHAT_SKILL_TEMPLATE_PATH.parent),
    trim_blocks=True,
    lstrip_blocks=True,
)

# Biến thể persona theo người đang trò chuyện (xem load_chat_skill):
# - "owner": chủ bot (Telegram/Zoom, ALLOWED_USER_ID) - có hồ sơ user_profile.
# - "zalo_admin": người dùng Zalo được cấp quyền quản trị - KHÔNG hồ sơ chủ bot.
# - "member": thành viên Zalo thường - không hồ sơ, phân tích chỉ để tham khảo.
PERSONA_OWNER = "owner"
PERSONA_ZALO_ADMIN = "zalo_admin"
PERSONA_MEMBER = "member"
PERSONA_VARIANTS = (PERSONA_OWNER, PERSONA_ZALO_ADMIN, PERSONA_MEMBER)

# Gợi ý trình bày theo kênh, nối SAU persona đã cache (không nằm trong cache):
# Zalo chỉ hiện chữ thuần (channels/zalo_text.py bỏ được tiêu đề/đậm/gạch đầu
# dòng nhưng KHÔNG bỏ được bảng, link markdown); Telegram đổi markdown-lite sang
# HTML (tg_format.py) nhưng không có bảng/tiêu đề #; Zoom đổi GFM sang markdown
# Zoom (channels/zoom.py) nhưng bảng dễ vỡ trên điện thoại.
_CHANNEL_STYLE_HINTS = {
    "zalo": (
        "KÊNH ĐANG TRẢ LỜI: Zalo - chỉ hiển thị chữ thuần. Viết ngắn gọn như nhắn tin; "
        "không dùng bảng, tiêu đề (#), chữ đậm hay link dạng [chữ](url) - cần liệt kê thì "
        "xuống dòng với dấu •."
    ),
    "telegram": (
        "KÊNH ĐANG TRẢ LỜI: Telegram - dùng markdown nhẹ (chữ đậm, gạch đầu dòng) khi cần; "
        "không dùng bảng hay tiêu đề kiểu #, muốn tách mục thì dùng chữ đậm."
    ),
    "zoom": (
        "KÊNH ĐANG TRẢ LỜI: Zoom - dùng markdown nhẹ (chữ đậm, gạch đầu dòng) khi cần; "
        "không dùng bảng."
    ),
}

# Giá trị giữ chỗ trong user_profile (chủ bot chưa điền): "..." hoặc "vd: ..." -
# render nguyên văn sẽ khiến model tưởng tên anh là "...".
_PROFILE_PLACEHOLDERS = {"", "...", "…"}

# Cache theo (biến thể persona, cách xưng hô riêng): cách xưng hô chỉ áp cho
# biến thể Zalo và nằm trong danh sách đóng core/address.ADDRESS_FORMS, nên tối
# đa 1 + 2 * (len(ADDRESS_FORMS) + 1) chuỗi (~15 chuỗi x ~15 KB).
_chat_skill_cache: dict[tuple[str, str | None], str] = {}


def _profile_value_filled(value) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return text not in _PROFILE_PLACEHOLDERS and not text.lower().startswith("vd:")


def _filled_profile(user_profile: dict | None) -> dict:
    """Các mục user_profile chủ bot đã điền thật (bỏ "note" và mục còn giữ chỗ)."""
    return {
        key: value
        for key, value in (user_profile or {}).items()
        if key != "note" and _profile_value_filled(value)
    }


def _render_chat_skill(variant: str = PERSONA_OWNER, address_form: str | None = None) -> str:
    """Đọc + render persona từ đĩa. Không cache — xem load_chat_skill()."""
    try:
        raw = CHAT_SKILL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""

    if CHAT_SKILL_PATH.suffix.lower() not in (".yaml", ".yml"):
        return raw.strip()

    try:
        data = yaml.safe_load(raw)
        template = _CHAT_SKILL_ENV.get_template(_CHAT_SKILL_TEMPLATE_PATH.name)
        profile = _filled_profile(data["persona"].get("user_profile"))
        if variant == PERSONA_OWNER and not profile:
            logger.info(
                "chat_skill.yaml: persona.user_profile vẫn để trống ('...') - điền tên, công "
                "việc, phong cách để Lan Anh trò chuyện đúng với anh hơn."
            )
        return template.render(
            p=data["persona"],
            tv=data["tone_of_voice"],
            quirks=data.get("quirks") or {},
            mood=data.get("energy_and_mood") or {},
            continuity=data.get("continuity") or {},
            rules=data["rules"],
            cm=data["content_modes"],
            variant=variant,
            profile=profile,
            address=(
                {"user": address_form, "ai": address.self_word(address_form)}
                if address_form
                else None
            ),
        ).strip()
    except Exception:
        logger.exception("Lỗi parse/render chat_skill.yaml, dùng nội dung thô làm dự phòng.")
        return raw.strip()


# TELEGRAM_RICH_MESSAGES bật: Telegram tự dựng tiêu đề/bảng từ markdown (tg_format.py).
_TELEGRAM_RICH_STYLE_HINT = (
    "KÊNH ĐANG TRẢ LỜI: Telegram - hiển thị được markdown (chữ đậm, gạch đầu dòng, tiêu "
    "đề #, bảng nhỏ khi cần so sánh); vẫn ưu tiên trả lời gọn như nhắn tin."
)


def channel_style_hint(channel: str | None) -> str:
    """1 dòng gợi ý trình bày theo kênh (rỗng nếu kênh lạ/không truyền)."""
    key = (channel or "").strip().lower()
    if key == "telegram" and TELEGRAM_RICH_MESSAGES:
        return _TELEGRAM_RICH_STYLE_HINT
    return _CHANNEL_STYLE_HINTS.get(key, "")


def load_chat_skill(
    force_reload: bool = False,
    *,
    variant: str | None = None,
    include_owner_profile: bool = True,
    channel: str | None = None,
    address_form: str | None = None,
) -> str:
    """Nạp persona/rules cho chat tự nhiên. Định dạng mặc định là YAML có
    cấu trúc (chat_skill.yaml), render qua templates/chat_skill_prompt.j2
    thành system_instruction gửi cho provider. Nếu CHAT_SKILL_PATH trỏ tới
    file .txt (cấu hình cũ), đọc thẳng làm văn bản để tương thích ngược.

    Kết quả được cache sau lần gọi đầu tiên. Hàm này nằm trên đường đi của
    MọI tin nhắn chat (orchestrator.chat()), nên nếu không cache thì mỗi tin
    nhắn đều phải đọc đĩa + parse YAML + render lại Jinja — toàn bộ đều là
    việc lặp lại vô ích vì persona là file tĩnh, chỉ đổi khi deploy lại.

    Truyền force_reload=True để bỏ cache và đọc lại từ đĩa.

    ``variant`` (PERSONA_*): "owner" có hồ sơ chủ bot (user_profile);
    "zalo_admin" và "member" KHÔNG có - bắt buộc cho mọi người dùng không phải
    chủ bot, nếu không thông tin chủ bot tự điền sẽ lộ sang họ. Không truyền
    variant thì theo tham số cũ ``include_owner_profile`` (True = "owner",
    False = "member").

    ``channel`` ("telegram" | "zalo" | "zoom"): nối thêm 1 dòng gợi ý trình bày
    theo kênh SAU persona đã cache (gợi ý không nằm trong cache).

    ``address_form`` (core/address.py, vd "chị", "cô"): cách gọi riêng do chủ
    bot đặt cho 1 thành viên Zalo - chỉ áp cho biến thể Zalo, giá trị ngoài
    danh sách bị bỏ qua. Biến thể chủ bot luôn giữ address_form của YAML.
    """
    if variant is None:
        variant = PERSONA_OWNER if include_owner_profile else PERSONA_MEMBER
    elif variant not in PERSONA_VARIANTS:
        # Giá trị lạ: KHÔNG bao giờ rơi về biến thể chủ bot (lộ hồ sơ).
        logger.warning("Biến thể persona %r không hợp lệ, dùng 'member'.", variant)
        variant = PERSONA_MEMBER
    form = None if variant == PERSONA_OWNER else address.normalize(address_form)
    if force_reload:
        _chat_skill_cache.clear()
    key = (variant, form)
    cached = _chat_skill_cache.get(key)
    if cached is None:
        cached = _render_chat_skill(variant, form)
        _chat_skill_cache[key] = cached
    hint = channel_style_hint(channel)
    return f"{cached}\n\n{hint}" if cached and hint else cached


def is_owner_user_id(user_id: int) -> bool:
    """Chủ bot = ALLOWED_USER_ID (Telegram và Zoom dùng chung). Zalo user mang
    internal_user_id ÂM nên không bao giờ khớp."""
    return bool(ALLOWED_USER_ID) and user_id == ALLOWED_USER_ID


# ─── Zoom Team Chat (webhook, chữ ký, gửi tin, pairing) ─────────────────────
ZOOM_ENABLED = _env_bool("ZOOM_ENABLED", False)
ZOOM_CLIENT_ID = os.getenv("ZOOM_CLIENT_ID", "").strip()
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET", "").strip()
ZOOM_BOT_JID = os.getenv("ZOOM_BOT_JID", "").strip()
# ZOOM_VERIFICATION_TOKEN: cơ chế xác thực CŨ (app kiểu "General App + Chatbot").
ZOOM_VERIFICATION_TOKEN = os.getenv("ZOOM_VERIFICATION_TOKEN", "").strip()
# ZOOM_SECRET_TOKEN: cơ chế MỚI (Access > Token > Secret Token trên Marketplace,
# đi cùng Event Subscriptions) — ưu tiên dùng cái này nếu app hỗ trợ.
ZOOM_SECRET_TOKEN = os.getenv("ZOOM_SECRET_TOKEN", "").strip()
ZOOM_ACCOUNT_ID = os.getenv("ZOOM_ACCOUNT_ID", "").strip()
ZOOM_WEBHOOK_PATH = "/webhook/zoom"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media").strip())

# Timeout cho các request tới Telegram Bot API, đặc biệt là tải ảnh/file.
# Render đôi khi đọc stream từ Telegram chậm hơn timeout mặc định của
# python-telegram-bot, dẫn tới telegram.error.TimedOut ở download_to_drive().
TELEGRAM_CONNECT_TIMEOUT = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30"))
TELEGRAM_READ_TIMEOUT = float(os.getenv("TELEGRAM_READ_TIMEOUT", "90"))
TELEGRAM_WRITE_TIMEOUT = float(os.getenv("TELEGRAM_WRITE_TIMEOUT", "90"))
TELEGRAM_POOL_TIMEOUT = float(os.getenv("TELEGRAM_POOL_TIMEOUT", "30"))
TELEGRAM_MEDIA_RETRIES = int(os.getenv("TELEGRAM_MEDIA_RETRIES", "3"))

# ─── Trải nghiệm Telegram (handlers/turn_feedback.py, tg_format.py) ──────────
# Lượt chat chạy quá chừng này giây thì gửi 1 dòng trạng thái giọng Lan Anh (câu
# trả lời sửa đè lên đúng dòng đó); 0 = không gửi.
TELEGRAM_STATUS_DELAY_SEC = max(0, min(60, _env_int("TELEGRAM_STATUS_DELAY_SEC", 7)))
# Thả 👀 lên tin phải xếp hàng sau lượt đang chạy, xong đổi 👌 (lỗi 😢).
TELEGRAM_BUSY_REACTIONS = _env_bool("TELEGRAM_BUSY_REACTIONS", True)
# Nút 👍/👎/🔄 Thử lại/✂️ Ngắn hơn dưới câu trả lời chat (chỉ câu mới nhất giữ nút).
TELEGRAM_ANSWER_BUTTONS = _env_bool("TELEGRAM_ANSWER_BUTTONS", True)
# Hiện chữ đang sinh dần bằng bản nháp (sendMessageDraft) cho lượt chat thường;
# tin cuối vẫn là bản đã lọc link/kiểm policy. Mặc định tắt.
TELEGRAM_STREAM_DRAFTS = _env_bool("TELEGRAM_STREAM_DRAFTS", False)
# Gửi câu trả lời bằng tin "rich" gốc (Bot API 10.1: tiêu đề, bảng, khối code do
# Telegram dựng); bị từ chối thì quay về HTML. Mặc định tắt tới khi chủ bot thấy
# app của mình hiển thị ổn.
TELEGRAM_RICH_MESSAGES = _env_bool("TELEGRAM_RICH_MESSAGES", False)


def ensure_media_dir() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/webhook"
DIAGNOSE_PATH = "/diagnose"

WEBHOOK_BASE_URL = (
    os.getenv("WEBHOOK_BASE_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
)

# Khoá mã hoá đối xứng (Fernet) dùng để mã hoá các giá trị nhạy cảm (vd
# __Secure-1PSIDTS đã rotate) trước khi lưu vào bảng settings trong DB. Tạo
# bằng: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Bắt buộc trong mọi runtime có DB: các giá trị nhạy cảm lưu trong settings
# (session Zalo, v.v.) không được phép hạ cấp về plaintext khi thiếu hoặc cấu
# hình sai khoá.
SETTINGS_ENC_KEY = os.getenv("SETTINGS_ENC_KEY", "").strip() or None
# Khoá cũ khi xoay khoá (nhiều khoá cách nhau dấu phẩy) - xem core/crypto.py.
SETTINGS_ENC_KEY_OLD = os.getenv("SETTINGS_ENC_KEY_OLD", "").strip() or None
# Từ chối giá trị settings chưa mã hoá (bật sau khi mọi secret đã có prefix enc:).
SETTINGS_REJECT_PLAINTEXT = _env_bool("SETTINGS_REJECT_PLAINTEXT", False)
# SSL tới Postgres: "require" (mặc định, mã hoá nhưng KHÔNG xác thực chứng chỉ
# server - hành vi cũ) hoặc "verify-full" (xác thực chứng chỉ + hostname; cần
# DATABASE_SSL_CA trỏ tới file CA của Supabase nếu CA đó không có trong hệ thống).
DATABASE_SSL_MODE = os.getenv("DATABASE_SSL_MODE", "require").strip().lower() or "require"
DATABASE_SSL_CA = os.getenv("DATABASE_SSL_CA", "").strip() or None
# Trần thời gian MỞ 1 kết nối DB mới (asyncpg mặc định 60s): Supabase treo/mất
# mạng thì lượt chat fail nhanh thay vì giữ khoá người dùng + slot
# MAX_CONCURRENT_TURNS cả phút cho mỗi câu lệnh.
DB_CONNECT_TIMEOUT_SEC = max(2, min(60, _env_int("DB_CONNECT_TIMEOUT_SEC", 8)))
# Circuit breaker DB (core/database.py): khi cả lần thử lại sau reconnect cũng
# mất kết nối (hoặc mở kết nối quá DB_CONNECT_TIMEOUT_SEC), coi DB sập trong
# chừng này giây - mọi lệnh DB báo lỗi ngay thay vì treo; hết hạn cho 1 lệnh
# thăm dò đi qua. 0 = tắt breaker.
DB_BREAKER_OPEN_SEC = max(0, min(600, _env_int("DB_BREAKER_OPEN_SEC", 30)))

# Secret riêng cho endpoint /diagnose (KHÔNG dùng chung với WEBHOOK_SECRET), truyền
# qua header X-Diagnose-Token thay vì query string để tránh lộ qua access log.
DIAGNOSE_SECRET = os.getenv("DIAGNOSE_SECRET", "").strip() or None

# Đăng nhập trang /admin (xem web.py). ADMIN_PASS đồng thời làm khoá ký cookie
# phiên đăng nhập (HMAC) - đổi ADMIN_PASS sẽ tự vô hiệu mọi phiên đang mở.
ADMIN_USER = os.getenv("ADMIN_USER", "").strip() or None
ADMIN_PASS = os.getenv("ADMIN_PASS", "").strip() or None
ADMIN_SESSION_TTL_SEC = _env_int("ADMIN_SESSION_TTL_SEC", 12 * 3600)


def validate(require_webhook: bool = False) -> None:
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not ALLOWED_USER_ID:
        missing.append("ALLOWED_USER_ID")
    if not ROUTER9_API_KEY and not (GOOGLE_AI_STUDIO_API_KEY_1 or GOOGLE_AI_STUDIO_API_KEY_2):
        missing.append("ROUTER9_API_KEY (hoặc GOOGLE_AI_STUDIO_API_KEY_1/2)")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not SETTINGS_ENC_KEY:
        missing.append("SETTINGS_ENC_KEY")

    if require_webhook:
        if not WEBHOOK_SECRET:
            missing.append("WEBHOOK_SECRET")
        if not WEBHOOK_BASE_URL:
            missing.append(
                "WEBHOOK_BASE_URL (hoặc deploy trên Render để tự có RENDER_EXTERNAL_URL)"
            )

    if ZOOM_ENABLED and not (ZOOM_SECRET_TOKEN or ZOOM_VERIFICATION_TOKEN):
        missing.append(
            "ZOOM_SECRET_TOKEN (hoặc ZOOM_VERIFICATION_TOKEN nếu dùng cơ chế xác thực cũ)"
        )
    if ZOOM_ENABLED and not (ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET and ZOOM_BOT_JID):
        missing.append("ZOOM_CLIENT_ID/ZOOM_CLIENT_SECRET/ZOOM_BOT_JID (cần đủ để gửi tin Zoom)")

    if missing:
        raise RuntimeError(
            "Thiếu biến môi trường bắt buộc: "
            + ", ".join(missing)
            + "\nXem hướng dẫn trong README.md"
        )

    try:
        from cryptography.fernet import Fernet

        Fernet(SETTINGS_ENC_KEY.encode())
    except Exception as exc:
        raise RuntimeError(
            "SETTINGS_ENC_KEY không hợp lệ; cần Fernet key URL-safe base64 44 ký tự. "
            'Tạo bằng: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc
