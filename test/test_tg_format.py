"""tg_format: markdown -> HTML Telegram (tiêu đề, khối code, bảng), luật rơi về
chữ thuần (chỉ BadRequest) và đường tin rich gốc (TELEGRAM_RICH_MESSAGES)."""

import pytest
from telegram.error import BadRequest, TimedOut

import tg_format
from core import config


def test_inline_markdown_giu_nhu_cu():
    html = tg_format.markdown_to_html("**Đậm** và *nghiêng*, `a_b`, [web](https://x.vn/a_b)\n- ý 1")
    assert "<b>Đậm</b>" in html and "<i>nghiêng</i>" in html
    assert "<code>a_b</code>" in html
    assert '<a href="https://x.vn/a_b">web</a>' in html
    assert "• ý 1" in html
    # snake_case ngoài code không thành nghiêng.
    assert tg_format.markdown_to_html("foreign_net_vol") == "foreign_net_vol"


def test_tieu_de_thanh_dong_chu_dam():
    html = tg_format.markdown_to_html("## Tóm tắt **nhanh**\nNội dung\n#hashtag")
    assert html.splitlines()[0] == "<b>Tóm tắt nhanh</b>"
    assert "#hashtag" in html


def test_khoi_code_thanh_pre_khong_bi_nghieng():
    text = "Ví dụ:\n```python\nx = a_b * 2 * c  # <tag> & _y_\n```\nxong *nhé*"
    html = tg_format.markdown_to_html(text)
    assert (
        '<pre><code class="language-python">x = a_b * 2 * c  # &lt;tag&gt; &amp; _y_</code></pre>'
        in html
    )
    assert "<i>nhé</i>" in html


def test_bang_thanh_pre_can_cot():
    text = "| Mã | Giá |\n|---|---:|\n| **FPT** | 120 |\n| HPG | 25,5 |\nHết."
    html = tg_format.markdown_to_html(text)
    assert html.startswith("<pre>") and html.endswith("</pre>\nHết.")
    body = html[len("<pre>") : html.index("</pre>")].splitlines()
    assert body[0] == "Mã  │ Giá"
    assert body[1].startswith("────")
    assert body[2] == "FPT │ 120"  # ** trong ô bảng bị bỏ
    assert "|" not in html


def test_cat_doan_giua_khoi_code_tu_dong_mo_lai():
    code = "\n".join(f"dòng {i} " + "x" * 40 for i in range(40))
    chunks = tg_format._split_raw_text(f"```\n{code}\n```", 1000)
    assert len(chunks) > 1
    for chunk in chunks:
        html = tg_format.markdown_to_html(chunk)
        assert html.count("<pre>") == 1 and "```" not in html


def test_html_to_plain_tra_lai_ky_tu_goc():
    assert tg_format.html_to_plain("<b>P&amp;L</b> &lt;5%") == "P&L <5%"


class _Message:
    def __init__(self, fail_html=None):
        self.sent: list[tuple] = []
        self.fail_html = fail_html
        self.chat_id = 7
        self.message_id = 99

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        if parse_mode == "HTML" and self.fail_html is not None:
            raise self.fail_html
        self.sent.append((text, parse_mode, reply_markup))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        if parse_mode == "HTML" and self.fail_html is not None:
            raise self.fail_html
        self.sent.append(("edit", text, parse_mode, reply_markup))


@pytest.mark.asyncio
async def test_bad_request_moi_roi_ve_chu_thuan_khong_con_entity():
    message = _Message(fail_html=BadRequest("Can't parse entities"))
    await tg_format.reply_rich(message, "**P&L** quý 3")
    assert message.sent == [("P&L quý 3", None, None)]


@pytest.mark.asyncio
async def test_timeout_khong_gui_lai_tranh_trung_tin():
    message = _Message(fail_html=TimedOut())
    with pytest.raises(TimedOut):
        await tg_format.reply_rich(message, "chào anh")
    assert message.sent == []


@pytest.mark.asyncio
async def test_nut_bam_chi_gan_doan_cuoi():
    message = _Message()
    long_text = "\n\n".join("đoạn " + "y" * 900 for _ in range(6))
    await tg_format.reply_rich(message, long_text, max_len=2000, reply_markup="KB")
    assert len(message.sent) > 1
    assert [markup for _, _, markup in message.sent] == [None] * (len(message.sent) - 1) + ["KB"]


@pytest.mark.asyncio
async def test_edit_first_plain_fallback_van_la_edit():
    message = _Message(fail_html=BadRequest("Can't parse entities"))
    await tg_format.reply_rich_edit_first(message, "**x** & y", reply_markup="KB")
    assert message.sent == [("edit", "x & y", None, "KB")]


class _Bot:
    def __init__(self, fail=None):
        self.calls: list[tuple] = []
        self.fail = fail
        self.plain: list[tuple] = []

    async def do_api_request(self, endpoint, api_kwargs=None, return_type=None):
        self.calls.append((endpoint, api_kwargs))
        if self.fail is not None:
            raise self.fail
        return True

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.plain.append((chat_id, text, parse_mode, reply_markup))


@pytest.mark.asyncio
async def test_tin_rich_goc_khi_bat_co(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_RICH_MESSAGES", True)
    bot = _Bot()
    await tg_format.send_rich(bot, 5, "## Bảng\n| a | b |\n|---|---|\n| 1 | 2 |", reply_markup="KB")
    endpoint, kwargs = bot.calls[0]
    assert endpoint == "sendRichMessage"
    assert kwargs["chat_id"] == 5 and kwargs["reply_markup"] == "KB"
    assert kwargs["rich_message"]["markdown"].startswith("## Bảng")
    assert bot.plain == []


@pytest.mark.asyncio
async def test_tin_rich_bi_tu_choi_quay_ve_html(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_RICH_MESSAGES", True)
    bot = _Bot(fail=BadRequest("Method not found"))
    await tg_format.send_rich(bot, 5, "**chào**")
    assert bot.plain == [(5, "<b>chào</b>", "HTML", None)]


@pytest.mark.asyncio
async def test_tin_co_link_khong_di_duong_rich(monkeypatch):
    """Rich message không tắt được link preview -> tin có link luôn đi HTML."""
    monkeypatch.setattr(config, "TELEGRAM_RICH_MESSAGES", True)
    bot = _Bot()
    await tg_format.send_rich(bot, 5, "xem https://ke-xau.vn/?q=1")
    assert bot.calls == [] and bot.plain


@pytest.mark.asyncio
async def test_co_tat_thi_khong_goi_rich(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_RICH_MESSAGES", False)
    bot = _Bot()
    await tg_format.send_rich(bot, 5, "## Tiêu đề")
    assert bot.calls == [] and bot.plain == [(5, "<b>Tiêu đề</b>", "HTML", None)]


@pytest.mark.asyncio
async def test_edit_first_rich_goc(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_RICH_MESSAGES", True)
    bot = _Bot()
    message = _Message()
    message.get_bot = lambda: bot
    await tg_format.reply_rich_edit_first(message, "# Xong", reply_markup="KB")
    endpoint, kwargs = bot.calls[0]
    assert endpoint == "editMessageText"
    assert kwargs["message_id"] == 99 and kwargs["chat_id"] == 7
    assert message.sent == []


def test_goi_y_kenh_telegram_theo_co_rich(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_RICH_MESSAGES", False)
    assert "không dùng bảng" in config.channel_style_hint("telegram")
    monkeypatch.setattr(config, "TELEGRAM_RICH_MESSAGES", True)
    assert "bảng nhỏ" in config.channel_style_hint("telegram")
