"""Hỏi đáp về ảnh (mô tả, đọc chữ, dịch, ảnh chụp màn hình...) thay vì luôn
viết prompt tạo ảnh.

``classify_caption()`` quyết định tất định (không gọi AI) theo thứ tự:

1. Từ khoá prompt/khoá danh tính/chỉnh sửa ảnh ("prompt", "tạo ảnh", "vẽ lại",
   "giống ảnh này", "giữ mặt", "cô gái 20", "giữ trang phục", "đổi nền", "mặc áo
   dài"...) -> viết prompt như cũ.
2. Câu hỏi/yêu cầu rõ ràng về ảnh ("dịch", "đọc", "là gì", "bao nhiêu", "giải
   thích", "tóm tắt", "mô tả", dấu "?" cuối câu...) -> hỏi đáp.
3. Còn lại, kể cả KHÔNG caption -> viết prompt (dạng 1 đã có test của chủ bot;
   PHOTO_NO_CAPTION_MODE=describe đổi riêng trường hợp không caption).

Caption "hoá đơn"/"bill"/"chi tiêu" được nơi gọi xử lý TRƯỚC (đọc hoá đơn ->
khoản chi nháp, services/expense_service.py).

Hỏi đáp: persona Lan Anh rút gọn + giờ VN + câu hỏi qua orchestrator.analyze_image
(provider-chain, hạn chót lượt). Đọc/dịch chữ chạy trong allow_cjk_output() - chép
chữ Nhật/Trung/Hàn không bị coi là output lẫn ngôn ngữ (và không làm 9Router bị
khai tử oan). Câu trả lời có dấu hiệu biểu đồ giá thì qua bộ lọc tất định bỏ dòng
khuyến nghị mua/bán, vùng mua, cắt lỗ, mục tiêu, tỷ trọng (stock/policy.py mới là
nơi quyết định action). Lượt được lưu lịch sử (kind "vision", không trích trí nhớ).
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import nullcontext
from dataclasses import dataclass

from ai import official_client, orchestrator
from core.envutil import env_on
from core.text_normalize import fold, nfc
from services import conversation_log, media_text, reply_audience
from services.prompt_identity import (
    GIRL_KEYWORDS,
    KEEP_FACE_KEYWORDS,
    OUTFIT_KEYWORDS,
    PRODUCT_KEYWORDS,
)
from services.prompt_templates import build_vision_instruction
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

MODE_PROMPT = "prompt"
MODE_QA = "qa"
DEFAULT_DESCRIBE_QUESTION = "Ảnh này có gì? Mô tả ngắn gọn giúp."
REPLY_MAX_CHARS = 6000


@dataclass(frozen=True)
class PhotoIntent:
    mode: str
    question: str = ""
    ocr: bool = False
    translate: bool = False
    chart: bool = False
    # Không caption mà PHOTO_NO_CAPTION_MODE=describe: thêm gợi ý gửi "prompt".
    describe_default: bool = False


def qa_enabled() -> bool:
    return env_on("VISION_QA_ENABLED")


def no_caption_mode() -> str:
    value = os.getenv("PHOTO_NO_CAPTION_MODE", MODE_PROMPT).strip().lower()
    return "describe" if value == "describe" else MODE_PROMPT


# (mẫu trên chữ thường CÒN DẤU, mẫu khi cả caption gõ không dấu) - như
# services/tools.side_effect_intent: bỏ dấu thì "đọc" trùng "dọc", "dịch" trùng "dích".
_PROMPT_RE = (
    re.compile(
        r"\bprompt\b|tạo (?:ảnh|hình)|vẽ (?:lại|thành|theo)|giống (?:ảnh|hình) này|"
        r"phong cách|\bstyle\b|biến thành|chuyển thành|làm thành|\bedit\b|chỉnh (?:ảnh|sửa)|"
        r"(?:đổi|thay|xoá|xóa|tách) (?:nền|phông|phông nền|background)|ghép (?:ảnh|vào)|"
        r"\bmặc (?:áo|đồ|váy|đầm|quần|vest|bikini|áo dài)|"
        r"(?:đổi|thay) (?:áo|đồ|váy|quần|tóc|kiểu tóc|màu)|\banime\b|\bchibi\b|ảnh thẻ"
    ),
    re.compile(
        r"\bprompt\b|tao (?:anh|hinh)|ve (?:lai|thanh|theo)|giong (?:anh|hinh) nay|"
        r"phong cach|\bstyle\b|bien thanh|chuyen thanh|lam thanh|\bedit\b|chinh (?:anh|sua)|"
        r"(?:doi|thay|xoa|tach) (?:nen|phong|background)|ghep (?:anh|vao)|"
        r"\bmac (?:ao|do|vay|dam|quan|vest|bikini)|(?:doi|thay) (?:ao|do|vay|quan|toc|mau)|"
        r"\banime\b|\bchibi\b|anh the"
    ),
)
_QA_RE = (
    re.compile(
        r"\bdịch\b|\bđọc\b|\bchép\b|là gì|là ai|là con gì|là cây gì|là món gì|là chữ gì|"
        r"bao nhiêu|giải thích|tóm tắt|\bmô tả\b|nhận xét|ý nghĩa|nghĩa là|có gì|viết gì|"
        r"ghi gì|chữ gì|ở đâu|nước nào|thế nào|ra sao|có đúng|có phải|kiểm tra|\bcheck\b|"
        r"xem (?:giúp|hộ|dùm|giùm)|phân tích|đánh giá|\bocr\b|translate|\bwhat\b|\bwhy\b|"
        r"\bhow\b|\?\s*$"
    ),
    re.compile(
        r"\bdich\b|\bdoc\b|\bchep\b|la gi|la ai|la con gi|la cay gi|la mon gi|bao nhieu|"
        r"giai thich|tom tat|\bmo ta\b|nhan xet|y nghia|nghia la|co gi|viet gi|ghi gi|chu gi|"
        r"o dau|nuoc nao|the nao|ra sao|co dung|co phai|kiem tra|\bcheck\b|xem (?:giup|ho|dum|gium)|"
        r"phan tich|danh gia|\bocr\b|translate|\bwhat\b|\bwhy\b|\bhow\b|\?\s*$"
    ),
)
_OCR_RE = (
    re.compile(r"\bđọc\b|\bchép\b|\bchữ\b|văn bản|\btext\b|\bocr\b|viết gì|ghi gì"),
    re.compile(r"\bdoc\b|\bchep\b|\bchu\b|van ban|\btext\b|\bocr\b|viet gi|ghi gi"),
)
_TRANSLATE_RE = (
    re.compile(r"\bdịch\b|translate|nghĩa là|tiếng việt là gì|nghĩa gì"),
    re.compile(r"\bdich\b|translate|nghia la|tieng viet la gi|nghia gi"),
)
_CHART_RE = (
    re.compile(
        r"biểu đồ|đồ thị|\bchart\b|\bnến\b|cổ phiếu|chứng khoán|\bmã\b|kỹ thuật|kháng cự|"
        r"\brsi\b|\bmacd\b|\bma\d+\b|vn-?index|xu hướng|\bsóng\b"
    ),
    re.compile(
        r"bieu do|do thi|\bchart\b|\bnen\b|co phieu|chung khoan|ky thuat|khang cu|\brsi\b|"
        r"\bmacd\b|\bma\d+\b|vn-?index|xu huong"
    ),
)
_IDENTITY_KEYWORDS = KEEP_FACE_KEYWORDS + GIRL_KEYWORDS + OUTFIT_KEYWORDS + PRODUCT_KEYWORDS


def _match(patterns: tuple[re.Pattern, re.Pattern], low: str, plain: bool) -> bool:
    return bool((patterns[1] if plain else patterns[0]).search(low))


def classify_caption(caption: str) -> PhotoIntent:
    """Chế độ xử lý ảnh theo caption (xem docstring module)."""
    text = nfc(caption or "").strip()
    low = re.sub(r"\s+", " ", text.lower())
    if not low:
        if qa_enabled() and no_caption_mode() == "describe":
            return PhotoIntent(MODE_QA, DEFAULT_DESCRIBE_QUESTION, describe_default=True)
        return PhotoIntent(MODE_PROMPT)
    if not qa_enabled():
        return PhotoIntent(MODE_PROMPT)
    plain = fold(low) == low
    if any(keyword in low for keyword in _IDENTITY_KEYWORDS) or _match(_PROMPT_RE, low, plain):
        return PhotoIntent(MODE_PROMPT)
    if not _match(_QA_RE, low, plain):
        return PhotoIntent(MODE_PROMPT)
    return PhotoIntent(
        MODE_QA,
        text,
        ocr=_match(_OCR_RE, low, plain),
        translate=_match(_TRANSLATE_RE, low, plain),
        chart=_match(_CHART_RE, low, plain),
    )


# ─── Bộ lọc khuyến nghị cổ phiếu trong câu trả lời về ảnh ────────────────────

_CHART_REPLY_RE = re.compile(
    r"biểu đồ|đồ thị|\bnến\b|kháng cự|hỗ trợ|\bRSI\b|\bMACD\b|\bMA\d+\b|khối lượng|cổ phiếu|"
    r"chứng khoán|xu hướng|đường trung bình|breakout|VN-?Index",
    re.IGNORECASE,
)
_CHART_NOTE = (
    "📌 Với ảnh biểu đồ {ai} chỉ mô tả thôi, không đưa khuyến nghị mua/bán, vùng mua, cắt lỗ "
    'hay mục tiêu. Muốn khuyến nghị theo hệ thống thì nhắn "phân tích <MÃ>" giúp {ai} nha{tail}.'
)


def guard_chart_reply(
    reply: str, *, force: bool = False, owner: bool = False, address_form: str | None = None
) -> str:
    """Bỏ mọi dòng có nhãn hành động/ngôn ngữ kế hoạch giao dịch khi câu trả lời
    (hoặc caption, ``force``) liên quan biểu đồ giá - không trông vào việc model
    nghe lời chỉ dẫn "không khuyến nghị"."""
    if not reply or not (force or _CHART_REPLY_RE.search(reply)):
        return reply
    from stock import report_format as rfmt

    kept: list[str] = []
    dropped = 0
    for line in reply.splitlines():
        if rfmt.find_action_conflicts(line, frozenset()) or rfmt.find_plan_language(line):
            dropped += 1
            continue
        kept.append(line)
    if not dropped:
        return reply
    logger.warning("Đã bỏ %d dòng khuyến nghị giao dịch trong câu trả lời về ảnh.", dropped)
    note = media_text.say(_CHART_NOTE, owner=owner, address_form=address_form)
    body = "\n".join(kept).strip()
    return f"{body}\n\n{note}" if body else note


# ─── Hỏi đáp ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VisionAnswer:
    text: str
    used_fallback: bool = False


_DESCRIBE_TIP = '(Muốn {ai} viết prompt tạo ảnh từ ảnh này thì gửi lại kèm chữ "prompt" nha{tail}.)'


async def answer(
    user_id: int,
    image_path: str,
    caption: str,
    intent: PhotoIntent,
    *,
    channel: str = "telegram",
) -> VisionAnswer:
    """Hỏi đáp 1 ảnh, lưu lịch sử. Raise khi provider-chain lỗi (nơi gọi trả câu
    lỗi giọng Lan Anh); câu trả lời rỗng -> VisionAnswer("")."""
    you, me, owner = media_text.persona_forms(user_id)
    question = intent.question or DEFAULT_DESCRIBE_QUESTION
    instruction = build_vision_instruction(
        question,
        you=you,
        me=me,
        time_line=official_client.now_vn_context(),
        ocr=intent.ocr,
        translate=intent.translate,
    )
    prompt_id = await telemetry.start(user_id, "vision", caption or question, channel=channel)
    allow_cjk = intent.ocr or intent.translate
    try:
        with orchestrator.allow_cjk_output() if allow_cjk else nullcontext():
            response = await orchestrator.analyze_image(instruction, image_path)
    except Exception as exc:
        await telemetry.failure(prompt_id, "vision", exc)
        raise
    text = (getattr(response, "text", None) or "").strip()[:REPLY_MAX_CHARS]
    form = reply_audience.current().address_form
    text = guard_chart_reply(text, force=intent.chart, owner=owner, address_form=form)
    await telemetry.success(prompt_id, "vision", text or "(không có nội dung)")
    if not text:
        return VisionAnswer("")
    if intent.describe_default:
        text += "\n\n" + media_text.say(_DESCRIBE_TIP, owner=owner, address_form=form)
    label = f"[Gửi ảnh] {caption}".strip()
    await conversation_log.record_exchange(user_id, label, text, conversation_log.KIND_VISION)
    return VisionAnswer(text, bool(getattr(response, "used_fallback", False)))
