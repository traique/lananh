"""channels/zalo_text: style zca-js (UTF-16) đi kèm chữ thuần GIỐNG HỆT bản cũ, và
đoạn cắt trùng split_for_zalo."""

import random
import re

import pytest

from channels import zalo_text
from services.channel_chat_service import split_for_zalo

# Bản to_plain_text trước khi có styles - làm chuẩn so sánh chữ thuần.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{2,3})(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_EXTRA_BLANK_LINES = re.compile(r"\n{3,}")


def _old_plain(text: str) -> str:
    cleaned = _HEADING.sub("", text or "")
    cleaned = _EMPHASIS.sub(r"\2", cleaned)
    cleaned = _BULLET.sub(r"\1• ", cleaned)
    cleaned = _EXTRA_BLANK_LINES.sub("\n\n", cleaned)
    return cleaned.strip()


_SAMPLES = [
    "### BÁO CÁO\n**Tóm tắt nhanh**\n* *Mã PPC:* mua quanh 8.8\n\n\n\nhết",
    "📊 **GEX**: **24.900 VND** (khớp lệnh realtime lúc 10:54)",
    "## **Tiêu đề đậm**  \nnội dung ***rất*** quan trọng\n- ý 1\n+ ý 2",
    "##\nfoo\n#hashtag đầu dòng",
    "a\n\n\n# T\n__gạch dưới__ và ___ba___",
    "  \n## \n**a\nb** cuối  \n\n",
    "không có markdown gì cả 😀 emoji 👍🏻",
    "",
]


@pytest.mark.parametrize("text", _SAMPLES)
def test_chu_thuan_giong_ban_cu(text):
    assert zalo_text.to_plain_text(text) == _old_plain(text)
    assert zalo_text.to_zalo_rich(text)[0] == _old_plain(text)


def test_chu_thuan_giong_ban_cu_voi_chuoi_ngau_nhien():
    rng = random.Random(7)
    alphabet = ["*", "**", "_", "__", "#", "## ", "- ", "+", "\n", "\n\n\n", " ", "chữ", "😀", "x"]
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 25)))
        assert zalo_text.to_plain_text(text) == _old_plain(text), repr(text)


def _styled(plain: str, styles: list[dict]) -> list[tuple[str, str]]:
    """Cắt chữ theo toạ độ UTF-16 như phía JavaScript."""
    units = plain.encode("utf-16-le")
    return [
        (units[s["start"] * 2 : (s["start"] + s["len"]) * 2].decode("utf-16-le"), s["st"])
        for s in styles
    ]


def test_dam_nghieng_tieu_de():
    plain, styles = zalo_text.to_zalo_rich("## Tóm tắt\n**FPT** tăng, *ổn* và ***rất mạnh***")
    assert plain == "Tóm tắt\nFPT tăng, ổn và rất mạnh"
    assert sorted(_styled(plain, styles)) == sorted(
        [("Tóm tắt", "b"), ("FPT", "b"), ("ổn", "i"), ("rất mạnh", "b"), ("rất mạnh", "i")]
    )
    assert all(set(s) == {"start", "len", "st"} for s in styles)


def test_toa_do_utf16_voi_emoji():
    plain, styles = zalo_text.to_zalo_rich("😀👍 **đậm**")
    assert styles == [{"start": 5, "len": 3, "st": "b"}]  # mỗi emoji = 2 đơn vị UTF-16
    assert _styled(plain, styles) == [("đậm", "b")]


def test_to_mau_phan_tram_chi_khi_bat():
    text = "FPT +2,5% | HPG -1.3% | VNM −0,8 % | mã-3%"
    assert zalo_text.to_zalo_rich(text)[1] == []
    plain, styles = zalo_text.to_zalo_rich(text, colorize=True)
    assert _styled(plain, styles) == [
        ("+2,5%", zalo_text.GREEN),
        ("-1.3%", zalo_text.RED),
        ("−0,8 %", zalo_text.RED),
    ]


def test_cat_doan_trung_split_for_zalo_va_doi_toa_do():
    paragraph = "**Đậm đầu đoạn** rồi chữ thường " + "x" * 300 + " *nghiêng cuối*"
    message = "\n\n".join(f"{i}. {paragraph}" for i in range(12))
    chunks, styles = zalo_text.zalo_rich_chunks([message, "## Ngắn"], limit=1000)
    assert chunks == split_for_zalo(zalo_text.to_plain_text(message), 1000) + ["Ngắn"]
    assert len(styles) == len(chunks)
    for chunk, chunk_styles in zip(chunks[:-1], styles[:-1], strict=True):
        for piece, st in _styled(chunk, chunk_styles):
            assert piece in ("Đậm đầu đoạn", "nghiêng cuối") or (
                piece and (paragraph.find(piece) >= 0)
            )
            assert st in ("b", "i")
    assert styles[-1] == [{"start": 0, "len": 4, "st": "b"}]


def test_style_bi_cat_giua_hai_doan_duoc_cat_theo():
    message = "**" + "a " * 700 + "a**"
    chunks, styles = zalo_text.zalo_rich_chunks([message], limit=1000)
    assert len(chunks) == 2
    for chunk, chunk_styles in zip(chunks, styles, strict=True):
        assert chunk_styles == [{"start": 0, "len": len(chunk), "st": "b"}]


def test_cat_doan_ngau_nhien_trung_split_for_zalo():
    rng = random.Random(3)
    words = ["**đậm**", "thường", "\n", "\n\n", "😀", "- ý", "## tiêu đề\n", "x" * 50]
    for _ in range(60):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(50, 400)))
        chunks, styles = zalo_text.zalo_rich_chunks([text], limit=300)
        assert chunks == split_for_zalo(zalo_text.to_plain_text(text), 300)
        for chunk, chunk_styles in zip(chunks, styles, strict=True):
            size = len(chunk.encode("utf-16-le")) // 2
            assert all(0 <= s["start"] and s["start"] + s["len"] <= size for s in chunk_styles)


@pytest.mark.asyncio
async def test_dispatch_zalo_kem_styles_va_to_mau_tin_co_phieu():
    from channels.contracts import ZaloMessageResponse
    from services import channel_dispatch
    from services.channel_result import ChannelResult

    results = iter(
        [ChannelResult(["**FPT** +2,5%"], market=True), ChannelResult(["chat **đậm** +3%"])]
    )
    saved: list[dict] = []

    async def chat(user_id, text, is_admin):
        return next(results)

    async def get_cached(*args):
        return None

    async def save_cached(account_id, event_id, kind, payload):
        saved.append(payload)

    handlers = channel_dispatch.DispatchHandlers(
        chat=chat,
        get_cached=get_cached,
        save_cached=save_cached,
        rich_chunk=zalo_text.zalo_rich_chunks,
    )
    payloads = []
    for event in ("m1", "m2"):
        request = channel_dispatch.DispatchRequest(
            account_id="acc",
            event_id=event,
            cache_kind="text",
            user_id=-1,
            is_admin=False,
            channel="zalo",
            text="hỏi",
            turn_key="zalo:-1",
        )
        payloads.append(await channel_dispatch.dispatch(request, handlers))
    stock, chat_payload = payloads
    assert stock["messages"] == ["FPT +2,5%"]
    assert stock["styles"] == [
        [{"start": 0, "len": 3, "st": "b"}, {"start": 4, "len": 5, "st": zalo_text.GREEN}]
    ]
    # Chat thường: không tô màu %, chỉ đậm.
    assert chat_payload["styles"] == [[{"start": 5, "len": 3, "st": "b"}]]
    assert saved == payloads
    response = ZaloMessageResponse.model_validate(stock)
    assert response.styles is not None and response.styles[0][1].st == "c_15a85f"
    # Bản cache cũ (trước khi có styles) vẫn đọc được.
    assert ZaloMessageResponse.model_validate({"messages": ["a"]}).styles is None


def test_payload_khong_co_style_thi_styles_none():
    from services.channel_dispatch import payload_from_result
    from services.channel_result import ChannelResult

    payload = payload_from_result(
        ChannelResult(["chữ thường"]), lambda m: m, zalo_text.zalo_rich_chunks
    )
    assert payload["messages"] == ["chữ thường"] and payload["styles"] is None
