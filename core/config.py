import logging
import os
from pathlib import Path

import jinja2
import yaml
from dotenv import load_dotenv

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
GOOGLE_AI_STUDIO_MODEL = os.getenv("GOOGLE_AI_STUDIO_MODEL", "gemini-3.5-flash").strip()

HAS_ANY_AI_STUDIO_KEY = bool(GOOGLE_AI_STUDIO_API_KEY_1 or GOOGLE_AI_STUDIO_API_KEY_2)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


# Timeout cho 1 lượt gọi 9Router (giữ tên biến ROUTER9_CALL_TIMEOUT_SEC; alias
# GEMINI_COOKIE_CALL_TIMEOUT_SEC cũ vẫn được đọc nếu ROUTER9_CALL_TIMEOUT_SEC
# chưa set riêng, để không phá cấu hình Render đã lưu sẵn từ trước).
ROUTER9_CALL_TIMEOUT_SEC = _env_int(
    "ROUTER9_CALL_TIMEOUT_SEC",
    _env_int("GEMINI_COOKIE_CALL_TIMEOUT_SEC", 30),
)
ROUTER9_MAX_CONCURRENCY = _env_int("ROUTER9_MAX_CONCURRENCY", 4)

# Groq — gateway OpenAI-compatible miễn phí, provider thứ 2 của provider-chain
# (router9 -> groq -> openrouter -> api1 -> api2). Model mặc định nằm trong
# free tier của Groq (console.groq.com); GROQ_REALTIME_MODEL (compound-mini)
# có tool tìm kiếm web tích hợp, chỉ dùng cho tác vụ require_real_search.
# Danh mục model free đổi theo thời gian - xem README mục Provider-chain nếu
# model mặc định ngừng hoạt động.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
# meta-llama/llama-4-scout-17b-16e-instruct (giá trị cũ) đã bị Groq khai tử
# (shutdown 17/07/2026, xem console.groq.com/docs/deprecations) - request sẽ
# lỗi 400/404 nếu còn dùng model đó. qwen/qwen3.6-27b hiện là model vision
# DUY NHẤT Groq liệt kê ở console.groq.com/docs/vision, miễn phí (free tier).
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b").strip()
GROQ_REALTIME_MODEL = os.getenv("GROQ_REALTIME_MODEL", "groq/compound-mini").strip()
GROQ_CALL_TIMEOUT_SEC = _env_int("GROQ_CALL_TIMEOUT_SEC", 30)
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
OPENROUTER_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", "google/gemma-4-26b-a4b-it:free").strip()
OPENROUTER_CALL_TIMEOUT_SEC = _env_int("OPENROUTER_CALL_TIMEOUT_SEC", 30)
OPENROUTER_MAX_CONCURRENCY = _env_int("OPENROUTER_MAX_CONCURRENCY", 4)

# ─── Provider-chain (router9 -> groq -> openrouter -> api1 -> api2) + trí nhớ
# hội thoại ────────────────────────────────────────────────────────────────
# 9Router chết -> chuyển hẳn sang provider kế tiếp (không thử lại router9 mỗi
# tin, chỉ có background probe + /userouter9 + đổi env mới kích hoạt thử lại
# router9). Groq/OpenRouter/API hết quota (429) -> cooldown cố định rồi tự
# thử lại (xem ai/provider_state.py).
CHAT_HISTORY_TURNS = _env_int("CHAT_HISTORY_TURNS", 8)
CHAT_SESSION_TIMEOUT_SEC = _env_int("CHAT_SESSION_TIMEOUT_SEC", 21600)  # 6 giờ
ROUTER9_PROBE_INTERVAL_SEC = _env_int(
    "ROUTER9_PROBE_INTERVAL_SEC", _env_int("COOKIE_PROBE_INTERVAL_SEC", 900)
)  # 15 phút
API_QUOTA_COOLDOWN_SEC = _env_int("API_QUOTA_COOLDOWN_SEC", 3600)  # 60 phút

# Thứ tự ưu tiên thử provider, đọc từ env PROVIDER_ORDER (vd "api1,api2,router9"
# để dùng API chính thức làm xương sống - xem README mục Provider-chain để
# cân nhắc trước khi đổi). Mặc định: router9 -> groq -> openrouter -> api1 ->
# api2 (toàn bộ chuỗi mặc định là các provider miễn phí, Gemini official đứng
# cuối làm lưới an toàn).
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
DAILY_DIGEST_HOUR_VN = _env_int("DAILY_DIGEST_HOUR_VN", 8)

CHAT_SKILL_PATH = Path(os.getenv("CHAT_SKILL_PATH", "chat_skill.yaml").strip())
# File tham chiếu văn phong/thuật ngữ dịch Nhật-Việt cho lệnh /dich (tùy chọn,
# fail-open nếu thiếu - xem services/translate_service.py::_reference_guide).
TRANSLATE_REFERENCE_PATH = os.getenv("TRANSLATE_REFERENCE_PATH", "translate_reference.txt").strip()
_CHAT_SKILL_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "chat_skill_prompt.j2"

# Jinja Environment dựng 1 lần ở module level (thay vì mỗi lần render), giống
# cách stock_analysis.py compile template của nó. Environment tự cache template
# đã compile bên trong.
_CHAT_SKILL_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(_CHAT_SKILL_TEMPLATE_PATH.parent),
    trim_blocks=True,
    lstrip_blocks=True,
)

_chat_skill_cache: str | None = None


def _render_chat_skill() -> str:
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
        return template.render(
            p=data["persona"],
            tv=data["tone_of_voice"],
            rules=data["rules"],
            cm=data["content_modes"],
        ).strip()
    except Exception:
        logger.exception("Lỗi parse/render chat_skill.yaml, dùng nội dung thô làm dự phòng.")
        return raw.strip()


def load_chat_skill(force_reload: bool = False) -> str:
    """Nạp persona/rules cho chat tự nhiên. Định dạng mặc định là YAML có
    cấu trúc (chat_skill.yaml), render qua templates/chat_skill_prompt.j2
    thành system_instruction gửi cho Gemini. Nếu CHAT_SKILL_PATH trỏ tới
    file .txt (cấu hình cũ), đọc thẳng làm văn bản để tương thích ngược.

    Kết quả được cache sau lần gọi đầu tiên. Hàm này nằm trên đường đi của
    MọI tin nhắn chat (orchestrator.chat() và _get_or_create_chat_gem()), nên
    nếu không cache thì mỗi tin nhắn đều phải đọc đĩa + parse YAML + dựng lại
    Jinja Environment — toàn bộ đều là việc lặp lại vô ích vì persona là file
    tĩnh, chỉ đổi khi deploy lại.

    Truyền force_reload=True để bỏ cache và đọc lại từ đĩa.
    """
    global _chat_skill_cache
    if _chat_skill_cache is None or force_reload:
        _chat_skill_cache = _render_chat_skill()
    return _chat_skill_cache


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


def ensure_media_dir() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/webhook"
DIAGNOSE_PATH = "/diagnose"

WEBHOOK_BASE_URL = (
    os.getenv("WEBHOOK_BASE_URL", "").strip()
    or os.getenv("RENDER_EXTERNAL_URL", "").strip()
)

# Khoá mã hoá đối xứng (Fernet) dùng để mã hoá các giá trị nhạy cảm (vd
# __Secure-1PSIDTS đã rotate) trước khi lưu vào bảng settings trong DB. Tạo
# bằng: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Bắt buộc trong mọi runtime có DB: các giá trị nhạy cảm lưu trong settings
# (session Zalo, v.v.) không được phép hạ cấp về plaintext khi thiếu hoặc cấu
# hình sai khoá.
SETTINGS_ENC_KEY = os.getenv("SETTINGS_ENC_KEY", "").strip() or None

# Secret riêng cho endpoint /diagnose (KHÔNG dùng chung với WEBHOOK_SECRET), truyền
# qua header X-Diagnose-Token thay vì query string để tránh lộ qua access log.
DIAGNOSE_SECRET = os.getenv("DIAGNOSE_SECRET", "").strip() or None


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
            "Tạo bằng: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        ) from exc
