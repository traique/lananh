import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import zalo_summary
from channels.zalo_text import to_plain_text

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
START = datetime(2026, 8, 4, 9, 0, tzinfo=VN_TZ)
END = datetime(2026, 8, 5, 9, 0, tzinfo=VN_TZ)


def _row(index: int, content: str):
    sent_at = datetime(2026, 8, 4, 10, index, tzinfo=VN_TZ)
    return (f"u{index}", f"Người {index}", content, sent_at)


def _patch_group(monkeypatch, rows):
    async def fake_resolve(account_id, target):
        return ("group-1", "congdong_4")

    async def fake_messages(account_id, group_id, start, end, limit):
        return rows

    monkeypatch.setattr(zalo_summary.zalo_repository, "resolve_group", fake_resolve)
    monkeypatch.setattr(zalo_summary.zalo_repository, "get_group_messages", fake_messages)


def test_to_plain_text_removes_markdown():
    raw = "### BÁO CÁO\n**Tóm tắt nhanh**\n* *Mã PPC:* mua quanh 8.8\n\n\n\nhết"
    out = to_plain_text(raw)
    assert "#" not in out
    assert "*" not in out
    assert "BÁO CÁO" in out
    assert "• Mã PPC: mua quanh 8.8" in out
    assert "\n\n\n" not in out


def test_to_plain_text_cleans_quote_message():
    raw = "📊 **GEX**: **24.900 VND** (khớp lệnh realtime lúc 10:54)"
    out = to_plain_text(raw)
    assert out == "📊 GEX: 24.900 VND (khớp lệnh realtime lúc 10:54)"


def test_section_titles_get_blank_line():
    body = "MÃ ĐƯỢC BÀN\n• VIX: giữ\nCẢNH BÁO VÀ TIN CẦN THEO DÕI\n• chợ phiên cuối tuần"
    out = zalo_summary._render(body)
    assert "• VIX: giữ\n\nCẢNH BÁO VÀ TIN CẦN THEO DÕI" in out


@pytest.mark.asyncio
async def test_verbatim_when_too_few_messages(monkeypatch):
    _patch_group(monkeypatch, [_row(1, "PPC quanh 8.8 dài hạn"), _row(2, "@All")])

    async def fail_ask(prompt):
        raise AssertionError("Không được gọi AI khi nhóm quá ít tin")

    monkeypatch.setattr(zalo_summary, "_ask", fail_ask)

    _, _, content = await zalo_summary.summarize_group("acc", "congdong_4", START, END)

    assert "NGUYÊN VĂN" in content
    assert "PPC quanh 8.8 dài hạn" in content
    assert "💬 2 tin nhắn" in content


@pytest.mark.asyncio
async def test_short_summary_is_sanitized(monkeypatch):
    _patch_group(monkeypatch, [_row(index, f"tin {index}") for index in range(1, 5)])
    prompts = []

    async def fake_ask(prompt):
        prompts.append(prompt)
        return "**MÃ ĐƯỢC BÀN**\n* PPC: mua quanh 8.8"

    monkeypatch.setattr(zalo_summary, "_ask", fake_ask)

    _, _, content = await zalo_summary.summarize_group("acc", "congdong_4", START, END)

    assert len(prompts) == 1
    assert "**" not in content
    assert "MÃ ĐƯỢC BÀN" in content
    assert "• PPC: mua quanh 8.8" in content


@pytest.mark.asyncio
async def test_long_group_uses_stock_prompt_and_plain_text(monkeypatch):
    _patch_group(monkeypatch, [_row(index, f"tin {index}") for index in range(1, 12)])
    prompts = []

    async def fake_ask(prompt):
        prompts.append(prompt)
        return "### BÁO CÁO\n**VIX** tăng trần"

    monkeypatch.setattr(zalo_summary, "_ask", fake_ask)

    _, _, content = await zalo_summary.summarize_group("acc", "congdong_4", START, END)

    assert len(prompts) == 2
    assert "MÃ ĐƯỢC BÀN" in prompts[-1]
    assert "Quyết định đã chốt" not in prompts[-1]
    assert "mã chỉ được nhắc tên" in prompts[-1]
    assert "ưu đãi lãi margin" in prompts[-1]
    assert "#" not in content
    assert "**" not in content


@pytest.mark.asyncio
async def test_today_discussion_no_messages(monkeypatch):
    _patch_group(monkeypatch, [])
    _, alias, parts = await zalo_summary.today_discussion("acc", "congdong_4")
    assert alias == "congdong_4"
    assert len(parts) == 1
    assert "chưa có tin nhắn nào hôm nay" in parts[0]


@pytest.mark.asyncio
async def test_today_discussion_returns_raw_transcript_no_ai(monkeypatch):
    _patch_group(monkeypatch, [_row(1, "PPC quanh 8.8"), _row(2, "VIX tăng trần")])

    async def fail_ask(prompt):
        raise AssertionError("/dangnoi không được gọi AI, chỉ trả nguyên văn")

    monkeypatch.setattr(zalo_summary, "_ask", fail_ask)

    group_id, alias, parts = await zalo_summary.today_discussion("acc", "congdong_4")
    assert group_id == "group-1"
    assert len(parts) == 1
    assert "PPC quanh 8.8" in parts[0]
    assert "VIX tăng trần" in parts[0]
    assert "THẢO LUẬN HÔM NAY" in parts[0]


@pytest.mark.asyncio
async def test_today_discussion_unknown_group_raises(monkeypatch):
    async def fake_resolve(account_id, target):
        return None

    monkeypatch.setattr(zalo_summary.zalo_repository, "resolve_group", fake_resolve)
    with pytest.raises(ValueError):
        await zalo_summary.today_discussion("acc", "khong-ton-tai")
