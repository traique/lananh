from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import facebook_caption as caption

SOURCE = "https://shopee.vn/product/1/2"
AFFILIATE = "https://s.shopee.vn/abc"


def test_strip_shopee_urls_keeps_other_links_and_trailing_punctuation():
    text = f"Giảm sâu: {SOURCE}. Xem thêm https://example.com/x và {AFFILIATE}, mua ngay"
    assert caption.strip_shopee_urls(text) == (
        "Giảm sâu: . Xem thêm https://example.com/x và , mua ngay"
    )


def test_build_caption_appends_cta_once_and_only_for_posts_with_links():
    body = f"Deal sốc\n\n\n{SOURCE}"
    assert caption.build_caption(body, True) == f"Deal sốc\n\n{caption.COMMENT_CTA}"
    assert caption.build_caption(f"Deal sốc\n\n{caption.COMMENT_CTA}", True).count(
        caption.COMMENT_CTA
    ) == 1
    assert caption.build_caption(body, False) == "Deal sốc"
    assert caption.build_caption(SOURCE, True) == caption.COMMENT_CTA


@pytest.mark.asyncio
async def test_rewrite_caption_never_leaves_a_shopee_link(monkeypatch):
    ask = AsyncMock(return_value=SimpleNamespace(text=f"Hời quá nè {AFFILIATE}"))
    monkeypatch.setattr(caption.orchestrator, "ask", ask)
    assert await caption.rewrite_caption(f"Deal {SOURCE}") == ("Hời quá nè", True)
    assert SOURCE not in ask.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [SimpleNamespace(text=""), RuntimeError("quota")])
async def test_rewrite_caption_falls_back_to_original_without_links(monkeypatch, answer):
    ask = AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock(return_value=answer)
    monkeypatch.setattr(caption.orchestrator, "ask", ask)
    assert await caption.rewrite_caption(f"Deal {SOURCE}") == ("Deal", False)
