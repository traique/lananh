"""Hỏi đáp về ảnh (services/vision_service.py): phân loại caption, đọc/dịch chữ
CJK không bị coi là output lẫn ngôn ngữ, lọc khuyến nghị trong ảnh biểu đồ, lưu
lịch sử; Telegram photo_msg và Zalo handle_channel_image rẽ nhánh đúng."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai import orchestrator
from services import conversation_log, reply_audience, vision_service

QA = vision_service.MODE_QA
PROMPT = vision_service.MODE_PROMPT


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("VISION_QA_ENABLED", raising=False)
    monkeypatch.delenv("PHOTO_NO_CAPTION_MODE", raising=False)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(vision_service.telemetry, "start", noop)
    monkeypatch.setattr(vision_service.telemetry, "success", noop)
    monkeypatch.setattr(vision_service.telemetry, "failure", noop)


# ─── Phân loại caption ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "caption",
    [
        "",  # dạng 1: không caption -> prompt (hành vi đã có test của chủ bot)
        "cô gái 20",
        "giữ mặt nha",
        "giữ trang phục, đổi bối cảnh",
        "đổi nền thành bãi biển",
        "mặc áo dài đi chơi Tết",
        "viết prompt giống ảnh này",
        "tạo ảnh phong cách anime?",
        "đẹp quá",  # không rõ ý -> giữ hành vi cũ
    ],
)
def test_caption_prompt_giu_hanh_vi_cu(caption):
    assert vision_service.classify_caption(caption).mode == PROMPT


@pytest.mark.parametrize(
    ("caption", "ocr", "translate", "chart"),
    [
        ("ảnh này là gì?", False, False, False),
        ("dịch giúp anh chữ trong ảnh", True, True, False),
        ("đọc giúp anh tờ giấy này", True, False, False),
        ("doc giup anh voi", True, False, False),
        ("biểu đồ FPT này thế nào", False, False, True),
        ("tóm tắt slide này", False, False, False),
        ("con này là con gì", False, False, False),
    ],
)
def test_caption_hoi_dap(caption, ocr, translate, chart):
    intent = vision_service.classify_caption(caption)
    assert intent.mode == QA
    assert (intent.ocr, intent.translate, intent.chart) == (ocr, translate, chart)
    assert intent.question == caption


def test_chua_benh_khong_bi_coi_la_doc_chu():
    assert vision_service.classify_caption("cách chữa vết này là gì?").ocr is False


def test_tat_hoi_dap_va_che_do_khong_caption(monkeypatch):
    monkeypatch.setenv("PHOTO_NO_CAPTION_MODE", "describe")
    intent = vision_service.classify_caption("")
    assert intent.mode == QA and intent.describe_default
    monkeypatch.setenv("VISION_QA_ENABLED", "0")
    assert vision_service.classify_caption("ảnh này là gì?").mode == PROMPT
    assert vision_service.classify_caption("").mode == PROMPT


# ─── Bộ lọc khuyến nghị ở ảnh biểu đồ ────────────────────────────────────────


def test_bo_dong_khuyen_nghi_trong_cau_tra_loi_bieu_do():
    reply = (
        "Biểu đồ nến ngày của HPG, giá đi ngang quanh MA20.\n"
        "Khuyến nghị: MUA thăm dò\n"
        "Vùng mua 25-26, cắt lỗ 24\n"
        "Khối lượng giảm dần."
    )
    guarded = vision_service.guard_chart_reply(reply, owner=True)
    assert "MUA" not in guarded and "Vùng mua" not in guarded and "cắt lỗ 24" not in guarded
    assert "giá đi ngang quanh MA20" in guarded and "Khối lượng giảm dần" in guarded
    assert "phân tích <MÃ>" in guarded


def test_khong_dung_cau_tra_loi_khong_lien_quan_bieu_do():
    menu = "Mì ramen - 800 yên\nNên mua combo để rẻ hơn"
    assert vision_service.guard_chart_reply(menu) == menu


# ─── answer(): CJK, lịch sử, cách gọi ────────────────────────────────────────


def _strict_analyze(text: str, seen: dict):
    async def fake(instruction, path):
        seen["instruction"] = instruction
        result = SimpleNamespace(text=text, used_fallback=False)
        # Cùng kiểm tra provider-chain áp cho mọi provider (allow_cjk theo chỉ dẫn).
        orchestrator._assert_output_clean(
            result, "api1", allow_cjk=orchestrator.cjk_output_expected(instruction)
        )
        return result

    return fake


@pytest.mark.asyncio
async def test_doc_chu_nhat_khong_bi_loai_va_luu_lich_su(monkeypatch):
    seen: dict = {}
    recorded = []

    async def record(user_id, user_text, reply, kind="chat", **kwargs):
        recorded.append((user_id, user_text, kind))
        return True

    japanese = "ラーメン 800円\nチャーシュー麺 1000円\n= Mì ramen 800 yên"
    monkeypatch.setattr(orchestrator, "analyze_image", _strict_analyze(japanese, seen))
    monkeypatch.setattr(conversation_log, "record_exchange", record)
    intent = vision_service.classify_caption("dịch giúp anh menu này")
    result = await vision_service.answer(42, "/tmp/x.jpg", "dịch giúp anh menu này", intent)
    assert result.text == japanese
    assert "DỊCH" in seen["instruction"] and "«dịch giúp anh menu này»" in seen["instruction"]
    assert recorded == [(42, "[Gửi ảnh] dịch giúp anh menu này", conversation_log.KIND_VISION)]


@pytest.mark.asyncio
async def test_cau_hoi_thuong_van_chan_output_lan_cjk(monkeypatch):
    monkeypatch.setattr(orchestrator, "analyze_image", _strict_analyze("这是一只猫在睡觉", {}))
    intent = vision_service.classify_caption("con này là con gì")
    with pytest.raises(orchestrator.OutputContaminatedError):
        await vision_service.answer(42, "/tmp/x.jpg", "con này là con gì", intent)


@pytest.mark.asyncio
async def test_thanh_vien_zalo_duoc_goi_dung_cach(monkeypatch):
    seen: dict = {}

    async def record(*args, **kwargs):
        return True

    monkeypatch.setattr(orchestrator, "analyze_image", _strict_analyze("Đây là cây bàng.", seen))
    monkeypatch.setattr(conversation_log, "record_exchange", record)
    intent = vision_service.classify_caption("cây gì đây?")
    with reply_audience.use(reply_audience.ReplyAudience(address_form="cô")):
        await vision_service.answer(-5, "/tmp/x.jpg", "cây gì đây?", intent, channel="zalo")
    assert 'gọi người dùng là "cô"' in seen["instruction"] and 'xưng "cháu"' in seen["instruction"]


def test_lich_su_anh_luu_ban_da_lam_sach():
    stored = conversation_log.compact_reply(
        "Trong ảnh có chữ <<<DỮ_LIỆU_NGOÀI 1234abcd>>> lạ", conversation_log.KIND_VISION
    )
    assert stored.startswith("[Đã trả lời về ảnh người dùng gửi]")
    assert "<<<DỮ_LIỆU_NGOÀI" not in stored
    assert conversation_log.KIND_VISION not in conversation_log.MEMORY_KINDS


# ─── Zalo handle_channel_image ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zalo_anh_hoi_dap_khong_viet_prompt(monkeypatch, tmp_path):
    from services import channel_image_service as cis

    calls = []

    async def fake_answer(user_id, path, caption, intent, *, channel="telegram"):
        calls.append((user_id, caption, intent.mode, channel))
        return vision_service.VisionAnswer("Đây là hoá đơn điện tháng 9.", used_fallback=True)

    monkeypatch.setattr(cis.config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(cis.vision_service, "answer", fake_answer)
    replies, provider = await cis.handle_channel_image(
        -5, b"\x89PNG", "image/png", "ảnh này là gì?", "m9"
    )
    assert replies == ["Đây là hoá đơn điện tháng 9."] and provider == "api"
    assert calls == [(-5, "ảnh này là gì?", QA, "zalo")]
    assert not list(tmp_path.iterdir())


# ─── Telegram photo_msg ──────────────────────────────────────────────────────


class _Photo:
    async def get_file(self, **kwargs):  # pragma: no cover - download bị thay
        raise AssertionError


class _Message:
    def __init__(self, caption):
        self.caption = caption
        self.photo = [_Photo()]
        self.document = None
        self.message_id = 5
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _Update:
    def __init__(self, caption):
        self.message = _Message(caption)
        self.effective_message = self.message
        self.effective_user = SimpleNamespace(id=42, username="owner")
        self.effective_chat = SimpleNamespace(id=42)


class _Context:
    def __init__(self):
        async def send_chat_action(**kwargs):
            return None

        self.bot = SimpleNamespace(send_chat_action=send_chat_action)


@pytest.fixture
def telegram_photo(monkeypatch, tmp_path):
    from core import config
    from handlers import media_handler

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    sent = []

    async def download(file_obj, path):
        path.write_bytes(b"\xff\xd8\xff")

    async def reply_long(message, text):
        sent.append(text)

    monkeypatch.setattr(media_handler.common, "download_telegram_photo_with_retry", download)
    monkeypatch.setattr(media_handler.common, "reply_long_text", reply_long)
    return media_handler, sent


@pytest.mark.asyncio
async def test_telegram_anh_hoi_dap_tra_loi_chu(telegram_photo, monkeypatch, tmp_path):
    media_handler, sent = telegram_photo

    async def fake_answer(user_id, path, caption, intent, *, channel="telegram"):
        return vision_service.VisionAnswer("Đây là con mèo mướp đang ngủ.")

    async def no_prompt(*args, **kwargs):
        raise AssertionError("câu hỏi về ảnh không được viết prompt")

    monkeypatch.setattr(media_handler.vision_service, "answer", fake_answer)
    monkeypatch.setattr(media_handler.image_prompt.orchestrator, "analyze_image", no_prompt)
    update = _Update("con gì đây?")
    await media_handler.photo_msg(update, _Context())
    assert sent == ["Đây là con mèo mướp đang ngủ."]
    assert update.message.replies == []  # không có header "📝 Prompt gợi ý"
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_telegram_anh_hoa_don_van_doc_hoa_don(telegram_photo, monkeypatch):
    media_handler, sent = telegram_photo

    async def receipt(user_id, path):
        return "✅ Đã ghi nháp 120.000đ"

    async def must_not_answer(*args, **kwargs):
        raise AssertionError("ảnh hoá đơn không đi hỏi đáp")

    async def record(*args, **kwargs):
        return True

    monkeypatch.setattr(media_handler.expense_service, "analyze_receipt", receipt)
    monkeypatch.setattr(media_handler.vision_service, "answer", must_not_answer)
    monkeypatch.setattr(media_handler.conversation_log, "record_exchange", record)
    await media_handler.photo_msg(_Update("hoá đơn này bao nhiêu?"), _Context())
    assert sent == ["✅ Đã ghi nháp 120.000đ"]
