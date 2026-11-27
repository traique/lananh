"""Luồng Zalo -> Facebook trên PostgreSQL thật (migration 032): tên nhóm nguồn, lọc
trùng GIỮA các nhóm (tra chính xác bằng index + so gần giống), ghi nhận nhóm đăng lại.

Opt-in như test_delivery_sql.py: TEST_PGLITE=1 hoặc TEST_DATABASE_URL (DB dùng một lần).
"""

import pytest

import test_delivery_sql as _delivery_sql
from channels import facebook_repository as repo
from services import facebook_dedup

sql_pool = _delivery_sql.sql_pool
delivery_db = _delivery_sql.delivery_db

DEAL = (
    "Nồi chiên không dầu 6 lít, lòng chống dính, hẹn giờ 60 phút, bảo hành 12 tháng, giá chỉ 899k"
)


async def _create(group_id, text, message_id, *, sender_id="u1"):
    return await repo.create_post(
        account_id="acc",
        group_id=group_id,
        sender_id=sender_id,
        sender_name="Người gửi",
        source_message_ids=[message_id],
        content=text,
        media=[],
        fingerprint=facebook_dedup.build_fingerprint(text, group_id=group_id, sender_id=sender_id),
    )


@pytest.mark.asyncio
async def test_ten_nhom_luu_va_cap_nhat(delivery_db):
    await repo.add_group("acc", "g1", "SanSale", "Săn Sale Giá Rẻ")
    await repo.add_group("acc", "g2", "mebim")
    # Thêm lại không kèm tên: giữ tên thật đã biết.
    await repo.add_group("acc", "g1", "sansale")

    groups = {g.group_id: g for g in await repo.list_group_details("acc")}
    assert groups["g1"].name == "Săn Sale Giá Rẻ" and groups["g1"].alias == "sansale"
    assert groups["g2"].name == ""

    changed = await repo.update_group_names(
        "acc", {"g1": "Săn Sale Giá Rẻ", "g2": "Hội Mẹ Bỉm", "g9": "Không phải nguồn"}
    )
    assert changed == 1  # g1 không đổi, g9 không phải nhóm nguồn
    groups = {g.group_id: g for g in await repo.list_group_details("acc")}
    assert groups["g2"].name == "Hội Mẹ Bỉm"
    assert await repo.list_groups("acc") == [("g2", "mebim"), ("g1", "sansale")]


@pytest.mark.asyncio
async def test_bai_trung_o_nhom_khac_bi_chan_va_ghi_nhan(delivery_db):
    pool = delivery_db
    await repo.add_group("acc", "g1", "sansale", "Săn Sale")
    await repo.add_group("acc", "g2", "mebim", "Mẹ Bỉm")
    await repo.add_group("acc", "g3", "congnghe", "Công Nghệ")

    first = await _create("g1", DEAL + " https://s.shopee.vn/AbC123", "m1")
    assert first is not None

    # Cùng link sản phẩm ở nhóm khác: chặn bằng tra chính xác (index GIN).
    with pytest.raises(repo.DuplicatePostError) as exc:
        await _create("g2", "Deal hời nè https://s.shopee.vn/AbC123", "m2", sender_id="u2")
    assert exc.value.match.post_id == first
    assert exc.value.match.group_id == "g1" and exc.value.match.cross_group

    # Đăng lại có thêm/bớt vài dòng ở nhóm thứ ba (không link): chặn bằng so gần giống.
    with pytest.raises(repo.DuplicatePostError) as exc:
        await _create("g3", "Chào cả nhà! " + DEAL + " Vào nhóm để nhận mã nha", "m3")
    assert exc.value.match.reason == facebook_dedup.REASON_CONTAINED

    # Lặp lại ở chính nhóm gốc: đếm thêm nhưng không thêm nhóm.
    with pytest.raises(repo.DuplicatePostError):
        await _create("g1", DEAL + " https://s.shopee.vn/AbC123 !!", "m4")

    row = await repo.get_post("acc", first)
    assert row["dup_count"] == 3
    assert list(row["dup_groups"]) == ["g2", "g3"]
    assert await pool.fetchval("SELECT count(*) FROM facebook_post_queue") == 1
    stored = await pool.fetchrow(
        "SELECT group_id, sender_id FROM facebook_post_fingerprints WHERE post_id = $1", first
    )
    assert (stored["group_id"], stored["sender_id"]) == ("g1", "u1")


@pytest.mark.asyncio
async def test_tra_chinh_xac_ngoai_pham_vi_quet_gan_giong(delivery_db, monkeypatch):
    """Bài gốc cũ hơn N bài gần nhất (FACEBOOK_DEDUP_SCAN_LIMIT) vẫn chặn được bản đăng
    lại cùng nội dung/sản phẩm nhờ tra index trên toàn cửa sổ ngày."""
    pool = delivery_db
    await repo.add_group("acc", "g1", "sansale")
    await repo.add_group("acc", "g2", "mebim")
    original = await _create("g1", DEAL + " https://s.shopee.vn/Old999", "m0")
    monkeypatch.setattr(facebook_dedup, "scan_limit", lambda: 2)
    for i in range(3):
        await _create(
            "g1", f"Sản phẩm khác số {i}, mô tả riêng biệt hoàn toàn, giá {100 + i}k", f"x{i}"
        )
    await pool.execute(
        "UPDATE facebook_post_fingerprints SET created_at = now() - interval '1 day' WHERE post_id = $1",
        original,
    )

    with pytest.raises(repo.DuplicatePostError) as exc:
        await _create("g2", "Có link nè https://s.shopee.vn/Old999", "m9", sender_id="u9")
    assert exc.value.match.post_id == original


@pytest.mark.asyncio
async def test_hang_cho_tra_ve_dau_van_tay_de_loc_lai(delivery_db):
    await repo.add_group("acc", "g1", "sansale")
    post_id = await _create("g1", DEAL, "m1")

    rows = await repo.list_pending_for_refilter("acc")
    assert [r["id"] for r in rows] == [post_id]
    assert rows[0]["group_id"] == "g1" and rows[0]["folded_text"]
    recent = await repo.recent_fingerprints("acc")
    assert [r["post_id"] for r in recent] == [post_id]


@pytest.mark.asyncio
async def test_tra_trung_chinh_xac_cho_loc_lai_hang_cho(delivery_db, monkeypatch):
    """/fb_loclai: exact_fingerprints tìm bài trùng nội dung/sản phẩm trên cả cửa sổ ngày,
    kể cả khi bài đó đã trôi khỏi FACEBOOK_DEDUP_SCAN_LIMIT bài gần nhất."""
    await repo.add_group("acc", "g1", "sansale")
    monkeypatch.setattr(facebook_dedup, "scan_limit", lambda: 1)
    original = await _create("g1", DEAL + " https://s.shopee.vn/Keep01", "m0")
    await _create("g1", "Bài khác hẳn, mô tả riêng biệt hoàn toàn, giá 101k", "m1")
    fp = facebook_dedup.build_fingerprint(DEAL + " https://s.shopee.vn/Keep01")

    assert [r["post_id"] for r in await repo.recent_fingerprints("acc")] != [original]
    by_hash = await repo.exact_fingerprints("acc", [fp.text_hash], [])
    by_product = await repo.exact_fingerprints("acc", [], fp.product_keys)
    assert [r["post_id"] for r in by_hash] == [original]
    assert [r["post_id"] for r in by_product] == [original]
    assert await repo.exact_fingerprints("acc", [], []) == []
