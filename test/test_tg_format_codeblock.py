"""Test cho tg_format_codeblock: khối <pre> phải giữ NGUYÊN VĂN prompt và
không bao giờ vượt giới hạn 4096 ký tự của Telegram sau khi escape."""
import asyncio

import tg_format_codeblock as cb


class FakeMessage:
    def __init__(self, fail_html=False):
        self.fail_html = fail_html
        self.sent = []

    async def reply_text(self, text, parse_mode=None):
        if self.fail_html and parse_mode == "HTML":
            raise RuntimeError("Bad Request: can't parse entities")
        self.sent.append((text, parse_mode))


def test_escaped_len_khop_voi_escape_that():
    text = "a & b < c > d"
    assert cb.escaped_len(text) == len(cb.escape_pre(text))


def test_wrap_boc_pre_va_escape():
    out = cb.wrap("1 < 2 & 3 > 0")
    assert out.startswith("<pre>") and out.endswith("</pre>")
    assert "&lt;" in out and "&amp;" in out and "&gt;" in out


def test_giu_nguyen_dau_sao_va_gach_duoi():
    """Đây là lý do chính phải dùng <pre> thay cho reply_rich(): prompt tiếng
    Anh chứa * và _ vẫn phải còn nguyên."""
    text = "raw photo, --ar 4:5, *not italic*, snake_case_name, **not bold**"
    out = cb.wrap(text)
    assert "*not italic*" in out
    assert "snake_case_name" in out
    assert "**not bold**" in out


def test_moi_chunk_sau_khi_boc_deu_nam_trong_gioi_han():
    text = ("Dòng prompt rất dài với ký tự & < > để test escape. " * 400)
    chunks = cb.split_for_code_block(text, max_len=4096)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(cb.wrap(chunk)) <= 4096


def test_khong_mat_noi_dung_khi_chia():
    text = "\n".join(f"dòng số {i} nội dung prompt" for i in range(400))
    chunks = cb.split_for_code_block(text, max_len=1000)
    joined = "".join(chunks).replace("\n", "")
    assert joined == text.replace("\n", "")


def test_dong_don_qua_dai_van_bi_cat_cung():
    text = "x" * 9000
    chunks = cb.split_for_code_block(text, max_len=4096)
    assert len(chunks) >= 3
    assert "".join(chunks) == text


def test_text_ngan_chi_tra_ve_mot_chunk():
    assert cb.split_for_code_block("prompt ngắn", max_len=4096) == ["prompt ngắn"]


def test_text_rong_khong_gui_gi():
    assert cb.split_for_code_block("   \n  ", max_len=4096) == []


def test_reply_code_block_gui_html_pre():
    msg = FakeMessage()
    asyncio.run(cb.reply_code_block(msg, "nội dung prompt"))
    assert len(msg.sent) == 1
    text, parse_mode = msg.sent[0]
    assert parse_mode == "HTML"
    assert text == "<pre>nội dung prompt</pre>"


def test_reply_code_block_roi_ve_plain_text_khi_html_loi():
    msg = FakeMessage(fail_html=True)
    asyncio.run(cb.reply_code_block(msg, "nội dung prompt"))
    assert len(msg.sent) == 1
    text, parse_mode = msg.sent[0]
    assert parse_mode is None
    assert text == "nội dung prompt"
