"""Luồng Zalo -> Facebook hiện TÊN nhóm thay cho group ID, lọc trùng GIỮA các nhóm
nguồn và bộ lọc bài không phải bài bán hàng (channels/facebook_group_names.py,
channels/facebook_commands.py, channels/facebook_intake.py)."""

import base64

import pytest

from channels import (
    facebook_commands,
    facebook_group_names,
    facebook_intake,
    facebook_repository,
)
from channels import router as zalo_router
from channels.contracts import ZaloFacebookPostRequest
from channels.facebook_repository import SourceGroup
from services import facebook_dedup, facebook_intake_stats
from services.facebook_caption import skip_reason

G1 = "7212345678901234501"
G2 = "7212345678901234502"
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
DEAL = (
    "Nồi chiên không dầu 6 lít, lòng chống dính, hẹn giờ 60 phút, "
    "bảo hành 12 tháng, giá chỉ 899k https://s.shopee.vn/AbC123"
)


@pytest.fixture(autouse=True)
def _reset_names_cache():
    facebook_group_names.invalidate()
    facebook_group_names._last_refresh.clear()
    yield
    facebook_group_names.invalidate()
    facebook_group_names._last_refresh.clear()


@pytest.fixture
def sources(monkeypatch):
    groups = [SourceGroup(G1, "sansale", "Săn Sale Giá Rẻ"), SourceGroup(G2, "mebim", "")]

    async def details(account_id):
        return list(groups)

    async def no_gateway():
        raise facebook_group_names.zalo_gateway_status.GatewayGroupsError("tắt")

    monkeypatch.setattr(facebook_repository, "list_group_details", details)
    monkeypatch.setattr(facebook_group_names.zalo_gateway_status, "list_groups", no_gateway)
    return groups


# ─── Tên nhóm ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_labels_prefer_real_group_name_then_alias(sources):
    names = await facebook_group_names.labels("acc")

    assert names == {G1: "Săn Sale Giá Rẻ", G2: "mebim"}
    assert facebook_group_names.label(names, "khong-co") == facebook_group_names.UNKNOWN


@pytest.mark.asyncio
async def test_missing_names_are_fetched_from_gateway_at_most_every_few_hours(monkeypatch, sources):
    calls = []

    async def gateway_groups():
        calls.append(1)
        group = facebook_group_names.zalo_gateway_status.ZaloGroup(G2, "Hội Mẹ Bỉm Sữa", 50)
        return [group], 1

    async def update(account_id, names):
        sources[1] = SourceGroup(G2, "mebim", names[G2])
        return 1

    monkeypatch.setattr(facebook_group_names.zalo_gateway_status, "list_groups", gateway_groups)
    monkeypatch.setattr(facebook_repository, "update_group_names", update)

    assert (await facebook_group_names.labels("acc"))[G2] == "Hội Mẹ Bỉm Sữa"
    facebook_group_names.invalidate()
    await facebook_group_names.labels("acc")
    assert len(calls) == 1  # đã có tên -> không hỏi lại gateway


@pytest.mark.asyncio
async def test_labels_never_fail_when_db_is_down(monkeypatch):
    async def broken(account_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(facebook_repository, "list_group_details", broken)

    assert await facebook_group_names.labels("acc") == {}


def test_find_group_by_alias_id_or_name_without_accents():
    groups = [SourceGroup(G1, "sansale", "Săn Sale Giá Rẻ"), SourceGroup(G2, "mebim", "")]

    assert facebook_group_names.find(groups, "sansale").group_id == G1
    assert facebook_group_names.find(groups, G2).group_id == G2
    assert facebook_group_names.find(groups, "san sale gia re").group_id == G1
    assert facebook_group_names.find(groups, "khác") is None


@pytest.mark.asyncio
async def test_fb_nhom_lists_names_not_ids(sources):
    result = await facebook_commands.maybe_handle_facebook_command("acc", "/fb_nhom")

    text = result.messages[0]
    assert "1. Săn Sale Giá Rẻ · gợi nhớ: sansale" in text
    assert f"2. {facebook_group_names.UNKNOWN} · gợi nhớ: mebim" in text
    assert G1 not in text and G2 not in text
    assert "lọc chung cho mọi nhóm" in text


@pytest.mark.asyncio
async def test_fb_xoanhom_accepts_group_name_and_confirms_with_name(monkeypatch, sources):
    removed = []

    async def remove(account_id, group_id):
        removed.append(group_id)
        return True

    async def no_refresh():
        return None

    monkeypatch.setattr(facebook_repository, "remove_group", remove)
    monkeypatch.setattr(facebook_commands.zalo_gateway_status, "refresh_groups", no_refresh)

    result = await facebook_commands.maybe_handle_facebook_command(
        "acc", "/fb_xoanhom san sale gia re"
    )

    assert removed == [G1]
    assert "“Săn Sale Giá Rẻ”" in result.messages[0]
    assert G1 not in result.messages[0]


@pytest.mark.asyncio
async def test_preview_shows_group_name_and_reposts_from_other_groups(monkeypatch, sources):
    row = {
        "id": 31,
        "status": "PENDING_APPROVAL",
        "original_content": "Deal nồi chiên 899k",
        "processed_content": "Deal nồi chiên 899k",
        "group_id": G1,
        "sender_name": "Lan",
        "sender_id": "u1",
        "dup_count": 3,
        "dup_groups": [G2],
    }

    async def get_post(account_id, post_id):
        return row

    async def no_media(post_id):
        return []

    async def no_links(account_id, urls):
        return {}

    monkeypatch.setattr(facebook_repository, "get_post", get_post)
    monkeypatch.setattr(facebook_repository, "get_media", no_media)
    monkeypatch.setattr(facebook_repository, "get_affiliate_links", no_links)

    preview = await facebook_commands._preview("acc", 31)

    assert "Nhóm: Săn Sale Giá Rẻ" in preview
    assert "🔁 Đã chặn 3 lần đăng lại, ở nhóm: mebim" in preview
    assert G1 not in preview and G2 not in preview


# ─── Lọc trùng giữa các nhóm ─────────────────────────────────────────────────


def _payload(text, group_id, sender_id="u1", message_id="m1"):
    return ZaloFacebookPostRequest(
        account_id="acc",
        group_id=group_id,
        sender_id=sender_id,
        sender_name="Người gửi",
        message_ids=[message_id],
        text=text,
        media=[{"mime_type": "image/png", "data_b64": PNG_B64}],
    )


@pytest.mark.asyncio
async def test_cross_group_duplicate_is_counted_per_group_with_names(monkeypatch, sources):
    async def duplicate(**kwargs):
        assert kwargs["fingerprint"].group_id == G2
        raise facebook_repository.DuplicatePostError(
            facebook_dedup.DuplicateMatch(12, facebook_dedup.REASON_CONTAINED, G1, True)
        )

    async def prepare(*args, **kwargs):
        raise AssertionError("bài trùng không được gửi xem trước")

    monkeypatch.setattr(zalo_router, "_secret", lambda: "s3cr3t")
    monkeypatch.setattr(facebook_repository, "create_post", duplicate)
    monkeypatch.setattr(facebook_commands, "prepare_post", prepare)
    facebook_intake_stats.reset()

    response = await zalo_router.facebook_group_post(_payload(DEAL, G2), "s3cr3t")
    await facebook_intake.drain()

    assert response.status_code == 204
    summary = facebook_intake_stats.summary()
    assert "🔁 Trùng với bài đã có ở nhóm khác: 1 bài" in summary
    assert "• mebim: 0 vào hàng chờ · 1 trùng (1 trùng nhóm khác)" in summary
    assert G2 not in summary


@pytest.mark.asyncio
async def test_queued_post_returns_before_preview_finishes(monkeypatch, sources):
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    prepared = []

    async def create(**kwargs):
        return 40

    async def prepare(account_id, post_id, note=None):
        started.set()
        await release.wait()
        prepared.append(post_id)

    async def nothing(*args, **kwargs):
        return []

    monkeypatch.setattr(zalo_router, "_secret", lambda: "s3cr3t")
    monkeypatch.setattr(facebook_repository, "create_post", create)
    monkeypatch.setattr(facebook_repository, "enforce_pending_cap", nothing)
    monkeypatch.setattr(facebook_repository, "prune_history", nothing)
    monkeypatch.setattr(facebook_commands, "prepare_post", prepare)

    response = await zalo_router.facebook_group_post(_payload(DEAL, G1), "s3cr3t")

    assert response.status_code == 204
    await asyncio.wait_for(started.wait(), 1)
    assert prepared == []  # AI viết lại/gửi xem trước chạy nền, gateway không phải chờ
    release.set()
    await facebook_intake.drain()
    assert prepared == [40]


@pytest.mark.asyncio
async def test_fb_loclai_rejects_cross_group_duplicates_in_queue(monkeypatch, sources):
    first = facebook_dedup.build_fingerprint(DEAL, group_id=G1, sender_id="a")
    repost_text = "Chào cả nhà! " + DEAL.replace("https://s.shopee.vn/AbC123", "") + " Vào nhóm nha"
    repost = facebook_dedup.build_fingerprint(repost_text, group_id=G2, sender_id="b")
    other = facebook_dedup.build_fingerprint(
        "Quạt mini để bàn 3 tốc độ, pin 2000mAh, giá 129k https://s.shopee.vn/Qq1",
        group_id=G2,
        sender_id="c",
    )

    def queue_row(post_id, fp, text):
        return {
            "id": post_id,
            "group_id": fp.group_id,
            "sender_id": fp.sender_id,
            "original_content": text,
            "media_count": 1,
            "text_hash": fp.text_hash,
            "product_keys": fp.product_keys,
            "image_hashes": fp.image_hashes,
            "folded_text": fp.folded_text,
        }

    def fp_row(post_id, fp):
        return {
            "post_id": post_id,
            "text_hash": fp.text_hash,
            "product_keys": fp.product_keys,
            "image_hashes": fp.image_hashes,
            "folded_text": fp.folded_text,
            "group_id": fp.group_id,
            "sender_id": fp.sender_id,
        }

    pending = [
        queue_row(50, first, DEAL),
        queue_row(51, repost, repost_text),
        queue_row(52, other, "Quạt mini để bàn 3 tốc độ, pin 2000mAh, giá 129k"),
    ]
    rejected, recorded = [], []

    async def list_pending(account_id):
        return pending

    async def recent(account_id):
        return [fp_row(52, other), fp_row(51, repost), fp_row(50, first)]

    async def exact(account_id, text_hashes, product_keys):
        return []

    async def reject(account_id, post_id):
        rejected.append(post_id)
        return True

    async def record(post_id, group_id):
        recorded.append((post_id, group_id))

    monkeypatch.setattr(facebook_repository, "list_pending_for_refilter", list_pending)
    monkeypatch.setattr(facebook_repository, "recent_fingerprints", recent)
    monkeypatch.setattr(facebook_repository, "exact_fingerprints", exact)
    monkeypatch.setattr(facebook_repository, "reject_post", reject)
    monkeypatch.setattr(facebook_repository, "record_duplicate", record)

    result = await facebook_commands.maybe_handle_facebook_command("acc", "/fb_loclai")

    assert rejected == [51]
    assert recorded == [(50, G2)]
    text = result.messages[0]
    assert "#51: trùng bài #50 ở nhóm Săn Sale Giá Rẻ" in text
    assert G1 not in text


# ─── Bộ lọc bài không phải bài bán hàng ───────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Ai có link mua cái nồi này không ạ?",
        "Cho mình xin link cái váy hoa với",
        "Tuyển CTV bán hàng online, thu nhập 10-15tr/tháng, inbox ngay",
        "Hỗ trợ vay tiền nhanh, nợ xấu vẫn vay được, giải ngân trong ngày",
    ],
)
def test_non_sale_posts_are_skipped(text):
    assert skip_reason(text, True) == "không phải bài bán hàng (hỏi đáp, tuyển dụng, vay tiền...)"


@pytest.mark.parametrize(
    "text",
    [
        "Váy hoa 199k, nhận sỉ lẻ, tuyển CTV toàn quốc",
        "Có ai dùng nồi này chưa, đang sale nè https://shopee.vn/noi-i.1.2",
        "Áo khoác nam chống nắng size M-XL, vải mát",
    ],
)
def test_real_sale_posts_are_kept(text):
    assert skip_reason(text, True) is None


def test_non_sale_filter_can_be_switched_off():
    assert skip_reason("Ai có link mua cái nồi này không ạ?", True, skip_non_sale=False) is None


@pytest.mark.asyncio
async def test_fb_loclai_finds_exact_duplicate_older_than_the_recent_scan(monkeypatch, sources):
    """Như lúc nhận bài: trùng chính xác (cùng link sản phẩm) với bài cũ đã trôi khỏi
    FACEBOOK_DEDUP_SCAN_LIMIT bài gần nhất vẫn bị bỏ khi /fb_loclai."""
    old = facebook_dedup.build_fingerprint(DEAL, group_id=G1, sender_id="a")
    repost_text = "Deal hời lắm mọi người ơi https://s.shopee.vn/AbC123"
    repost = facebook_dedup.build_fingerprint(repost_text, group_id=G2, sender_id="b")
    pending = [
        {
            "id": 90,
            "group_id": G2,
            "sender_id": "b",
            "original_content": repost_text,
            "media_count": 1,
            "text_hash": repost.text_hash,
            "product_keys": repost.product_keys,
            "image_hashes": [],
            "folded_text": repost.folded_text,
        }
    ]
    asked = []

    async def list_pending(account_id):
        return pending

    async def recent(account_id):
        return []  # bài gốc đã trôi khỏi phạm vi quét gần giống

    async def exact(account_id, text_hashes, product_keys):
        asked.append((list(text_hashes), list(product_keys)))
        return [
            {
                "post_id": 7,
                "text_hash": old.text_hash,
                "product_keys": old.product_keys,
                "image_hashes": [],
                "folded_text": old.folded_text,
                "group_id": G1,
                "sender_id": "a",
            }
        ]

    rejected = []

    async def reject(account_id, post_id):
        rejected.append(post_id)
        return True

    async def record(post_id, group_id):
        return None

    monkeypatch.setattr(facebook_repository, "list_pending_for_refilter", list_pending)
    monkeypatch.setattr(facebook_repository, "recent_fingerprints", recent)
    monkeypatch.setattr(facebook_repository, "exact_fingerprints", exact)
    monkeypatch.setattr(facebook_repository, "reject_post", reject)
    monkeypatch.setattr(facebook_repository, "record_duplicate", record)

    result = await facebook_commands.maybe_handle_facebook_command("acc", "/fb_loclai")

    assert asked == [([repost.text_hash], repost.product_keys)]
    assert rejected == [90]
    assert "trùng bài #7 ở nhóm Săn Sale Giá Rẻ" in result.messages[0]
