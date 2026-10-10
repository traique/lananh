"""Admin commands for the isolated Zalo -> Facebook publishing flow."""

import asyncio
import logging
import os
from typing import Awaitable, Callable
from urllib.parse import urlparse

import asyncpg

from channels import facebook_repository, zalo_repository
from services import facebook_caption, market_page
from services.channel_result import ChannelResult
from services.facebook_caption import find_shopee_urls
from services.facebook_image import brand_image


def _env_on(name: str) -> bool:
    """Bộ lọc mặc định bật; đặt biến = 0 để tắt (cùng quy ước với channels/router.py)."""
    return os.getenv(name, "1").strip() != "0"
from services.facebook_page_service import (
    FacebookPublishError,
    FacebookPublicationUncertain,
    configured_page_keys,
    inspect_page_post,
    post_comment,
    publish_page_post,
)

_ALLOWED_AFFILIATE_SCHEMES = {"http", "https"}
logger = logging.getLogger(__name__)
_admin_notification_callback: Callable[[str], Awaitable[None]] | None = None


def set_admin_notification_callback(
    callback: Callable[[str], Awaitable[None]] | None,
) -> None:
    global _admin_notification_callback
    _admin_notification_callback = callback


def _valid_shopee_affiliate_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return (
        parsed.scheme in _ALLOWED_AFFILIATE_SCHEMES
        and (parsed.hostname or "").lower() == "s.shopee.vn"
    )


async def _cached_affiliates(account_id: str, source_urls: list[str]) -> dict[str, str]:
    stored = await facebook_repository.get_affiliate_links(account_id, source_urls)
    return {src: aff for src, aff in stored.items() if _valid_shopee_affiliate_url(aff)}


async def _auto_rewrite(account_id: str, row) -> str | None:
    """Once every Shopee link has an affiliate link, replace the caption (still
    carrying source links) with a link-free AI rewrite. Returns an admin note
    when it ran; an already-cleaned or hand-edited caption is left alone."""
    urls = find_shopee_urls(row["original_content"])
    if not urls or not find_shopee_urls(row["processed_content"]):
        return None
    if len(await _cached_affiliates(account_id, urls)) < len(urls):
        return None
    caption, rewritten = await facebook_caption.rewrite_caption(row["processed_content"])
    await facebook_repository.update_content(account_id, row["id"], caption)
    if rewritten:
        return "✍️ AI đã viết lại bài; link affiliate sẽ được thả ở bình luận đầu tiên."
    return "⚠️ AI chưa viết lại được; giữ bài gốc đã lọc link (có thể /fb_sua rồi /fb_ok)."


async def _preview(account_id: str, post_id: int) -> str:
    row = await facebook_repository.get_post(account_id, post_id)
    if not row:
        return f"Không tìm thấy bài #{post_id}."
    media = await facebook_repository.get_media(post_id)
    source_urls = find_shopee_urls(row["original_content"])
    cached = await _cached_affiliates(account_id, source_urls)
    missing = [url for url in source_urls if url not in cached]
    lines = [
        f"📝 BÀI FACEBOOK CHỜ DUYỆT #{post_id}",
        f"Nhóm: {row['group_id']}",
        f"Người đăng: {row['sender_name'] or row['sender_id']}",
        f"Ảnh: {len(media)}",
        "",
        facebook_caption.build_caption(row["processed_content"], bool(source_urls), seed=post_id)
        or "(không có caption)",
    ]
    if cached:
        lines.extend([
            "", "💬 Bình luận đầu tiên:",
            facebook_caption.build_comment(
                [cached[url] for url in source_urls if url in cached], seed=post_id,
            ),
        ])
    if source_urls:
        lines.extend(["", "🔗 Link Shopee gốc (chưa chuyển đổi):", *source_urls])
    if missing:
        lines.extend(
            [
                "",
                "⚠️ Có link Shopee chưa được đổi sang affiliate của bạn.",
                "Tạo short-link trên Shopee Affiliate rồi nhập:",
                f"/fb_link {post_id} <link-affiliate>",
            ]
        )
    # One command per line: a renderer that treats _x_ as italics would eat the
    # underscores of two commands sharing a line ("/fbok ... /fbboqua").
    lines.extend(["", f"/fb_ok {post_id}", f"/fb_boqua {post_id}"])
    return "\n".join(lines)


async def prepare_post(account_id: str, post_id: int, note: str | None = None) -> None:
    row = await facebook_repository.get_post(account_id, post_id)
    if not row:
        return
    await _auto_rewrite(account_id, row)
    controller = os.getenv("ZALO_CONTROLLER_ID", "").strip()
    if not controller:
        from channels import zalo_session

        controller = await zalo_session.load_controller()
    preview = await _preview(account_id, post_id)
    if note:
        preview = f"{preview}\n\n{note}"
    if controller:
        await zalo_repository.enqueue_outbox(account_id, controller, preview)
    if _admin_notification_callback is not None:
        try:
            await _admin_notification_callback(preview)
        except Exception:
            logger.warning("Không gửi được preview Facebook tới kênh admin phụ.", exc_info=True)


async def maybe_handle_facebook_command(account_id: str, text: str) -> ChannelResult | None:
    raw = text.strip()
    command = raw.split(maxsplit=1)[0].lower() if raw else ""

    if command == "/fb_reset":
        deleted, sequence_reset = await facebook_repository.reset_posts(account_id)
        if sequence_reset:
            return ChannelResult([
                f"✅ Đã xóa {deleted} bài Facebook đã lưu. Bài mới tiếp theo sẽ bắt đầu lại từ #1."
            ])
        return ChannelResult([
            f"✅ Đã xóa {deleted} bài Facebook của tài khoản này. "
            "ID chưa thể về #1 vì vẫn còn bài của tài khoản Facebook/Zalo khác trong hàng đợi."
        ])

    if command == "/fb_loclai":
        # Chạy lại bộ lọc hiện tại cho các bài đang chờ (bài vào hàng chờ trước khi
        # bộ lọc được cải thiện). Bài bị lọc chuyển sang "bỏ qua" và xoá ảnh.
        rows = await facebook_repository.list_pending_for_refilter(account_id)
        removed: list[tuple[int, str]] = []
        for row in rows:
            reason = facebook_caption.skip_reason(
                row["original_content"] or "",
                int(row["media_count"] or 0) > 0,
                require_photo_and_caption=_env_on("FACEBOOK_REQUIRE_PHOTO_AND_CAPTION"),
                skip_voucher=_env_on("FACEBOOK_SKIP_VOUCHER_POSTS"),
            )
            if reason and await facebook_repository.reject_post(account_id, int(row["id"])):
                removed.append((int(row["id"]), reason))
        if not removed:
            return ChannelResult([f"✅ Đã kiểm tra {len(rows)} bài chờ: không có bài nào cần bỏ."])
        lines = [f"🧹 Đã bỏ {len(removed)}/{len(rows)} bài chờ không đạt bộ lọc:"]
        lines += [f"• #{post_id}: {reason}" for post_id, reason in removed[:30]]
        if len(removed) > 30:
            lines.append(f"... và {len(removed) - 30} bài khác.")
        return ChannelResult(["\n".join(lines)])

    if command == "/fb_boloc":
        from services import facebook_intake_stats

        if raw.lower().split()[1:] == ["reset"]:
            facebook_intake_stats.reset()
            return ChannelResult(["✅ Đã đặt lại thống kê bộ lọc."])
        pending = await facebook_repository.count_pending(account_id)
        return ChannelResult([
            facebook_intake_stats.summary()
            + f"\n\n📥 Đang chờ duyệt: {pending}/{facebook_repository.max_pending()} bài."
        ])

    if command == "/fb_nhom":
        groups = await facebook_repository.list_groups(account_id)
        if not groups:
            return ChannelResult(["Chưa có nhóm nguồn Facebook. Dùng /fb_themnhom <group_id> <tên-gợi-nhớ>."])
        lines = ["📣 Nhóm nguồn đăng Facebook:"]
        lines.extend(f"{i}. {alias} — {group_id}" for i, (group_id, alias) in enumerate(groups, 1))
        return ChannelResult(["\n".join(lines)])

    if command == "/fb_pages":
        pages = configured_page_keys()
        if not pages:
            return ChannelResult([
                "Chưa cấu hình Facebook Page nào. Đặt FACEBOOK_PAGE_ID + FACEBOOK_PAGE_ACCESS_TOKEN "
                "cho page mặc định, hoặc FACEBOOK_PAGE_ID_<key> + FACEBOOK_PAGE_ACCESS_TOKEN_<key> "
                "cho page bổ sung (ví dụ key=2)."
            ])
        lines = [
            "📄 Facebook Page đã cấu hình — /fb_ok sẽ đăng lên TẤT CẢ các page này:",
        ]
        lines.extend(f"- {key}" for key in pages)
        return ChannelResult(["\n".join(lines)])

    if command == "/fb_market":
        parts = raw.lower().split()
        if len(parts) < 2 or parts[1] not in ("stock", "news") or parts[2:] not in ([], ["dang"]):
            return ChannelResult([
                "Cú pháp: /fb_market <stock|news> [dang]\n"
                "- stock: báo cáo VN-INDEX; news: tin CafeF\n"
                "- không có 'dang': chỉ tạo nội dung để xem thử, KHÔNG đăng\n"
                "- thêm 'dang': đăng thật lên Page chứng khoán (FACEBOOK_PAGE_ID_MARKET)"
            ])
        job, publish = parts[1], parts[2:] == ["dang"]
        try:
            text = await market_page.run_manual(job, publish=publish)
        except market_page.MarketPageError as exc:
            return ChannelResult([f"❌ {exc}"])
        except FacebookPublicationUncertain:
            return ChannelResult([
                "⚠️ Chưa rõ Facebook đã tạo bài hay chưa. Kiểm tra Page chứng khoán trước khi chạy lại."
            ])
        except Exception as exc:
            logger.warning("/fb_market %s lỗi (%s).", job, type(exc).__name__, exc_info=True)
            return ChannelResult([f"❌ Chạy {job} thất bại: {type(exc).__name__}: {exc}"])
        if not text:
            return ChannelResult([f"Không có nội dung để đăng cho {job} (xem log để biết lý do)."])
        if publish:
            return ChannelResult([f"✅ Đã đăng {job} lên Page chứng khoán:\n\n{text}"])
        return ChannelResult([
            f"👀 XEM THỬ {job} (chưa đăng):\n\n{text}\n\nĐăng thật: /fb_market {job} dang"
        ])

    if command == "/fb_themnhom":
        parts = raw.split(maxsplit=2)
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /fb_themnhom <group_id> <tên-gợi-nhớ>"])
        group_id = parts[1].strip()
        alias = (parts[2].strip() if len(parts) == 3 else group_id).lower()
        if not group_id or not alias or len(alias) > 100:
            return ChannelResult(["Group ID hoặc tên gợi nhớ không hợp lệ."])
        try:
            await facebook_repository.add_group(account_id, group_id, alias)
        except asyncpg.UniqueViolationError:
            return ChannelResult([f"Tên gợi nhớ “{alias}” đang được dùng cho nhóm Facebook khác."])
        return ChannelResult([f"✅ Đã thêm nhóm Facebook {alias} ({group_id}). Không ảnh hưởng /tongket."])

    if command == "/fb_xoanhom":
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /fb_xoanhom <group_id hoặc tên-gợi-nhớ>"])
        target = parts[1].strip()
        removed = await facebook_repository.remove_group(account_id, target)
        if not removed:
            return ChannelResult([f"Không tìm thấy nhóm Facebook “{target}”."])
        return ChannelResult([f"✅ Đã xóa nhóm Facebook {target}. Danh sách /tongket không thay đổi."])

    if command == "/fb_xem":
        parts = raw.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            return ChannelResult(["Cú pháp: /fb_xem <post_id>"])
        return ChannelResult([await _preview(account_id, int(parts[1]))])

    if command == "/fb_sua":
        parts = raw.split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit():
            return ChannelResult(["Cú pháp: /fb_sua <post_id> <nội dung mới>"])
        post_id = int(parts[1])
        if not await facebook_repository.update_content(account_id, post_id, parts[2].strip()):
            return ChannelResult([f"Không thể sửa bài #{post_id} (không tồn tại hoặc đã xử lý)."])
        return ChannelResult([await _preview(account_id, post_id)])

    if command == "/fb_link":
        parts = raw.split()
        if len(parts) not in {3, 4} or not parts[1].isdigit():
            return ChannelResult([
                "Cú pháp: /fb_link <post_id> <affiliate_url> (bài có 1 link Shopee)\n"
                "hoặc: /fb_link <post_id> <source_url> <affiliate_url> (bài có nhiều link)\n"
                "Link affiliate phải là short-link dạng https://s.shopee.vn/..."
            ])
        post_id = int(parts[1])
        row = await facebook_repository.get_post(account_id, post_id)
        if not row or row["status"] not in {"PENDING_APPROVAL", "ERROR"}:
            return ChannelResult([f"Không thể cập nhật link cho bài #{post_id}."])
        urls = find_shopee_urls(row["original_content"])
        if not urls:
            return ChannelResult([f"Bài #{post_id} không có link Shopee cần thay."])

        # With >1 source URL, require explicit source mapping so one product's
        # affiliate link can never overwrite all products in a post.
        if len(parts) == 3:
            if len(urls) != 1:
                return ChannelResult([
                    f"Bài #{post_id} có {len(urls)} link Shopee. "
                    f"Nhập từng link: /fb_link {post_id} <source_url> <affiliate_url>."
                ])
            source_url, affiliate_url = urls[0], parts[2]
        else:
            source_url, affiliate_url = parts[2], parts[3]
            if source_url not in urls:
                return ChannelResult(["source_url không thuộc bài Facebook này."])

        if not _valid_shopee_affiliate_url(affiliate_url):
            return ChannelResult([
                "Link affiliate phải là short-link chính thức dạng https://s.shopee.vn/..."
            ])
        await facebook_repository.set_affiliate_link(account_id, source_url, affiliate_url)
        note = await _auto_rewrite(account_id, row)
        saved = f"✅ Đã lưu short affiliate link chính thức cho bài #{post_id}: {affiliate_url}"
        return ChannelResult([
            "\n".join(filter(None, [saved, note])) + f"\n\n{await _preview(account_id, post_id)}"
        ])

    if command == "/fb_boqua":
        parts = raw.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            return ChannelResult(["Cú pháp: /fb_boqua <post_id>"])
        post_id = int(parts[1])
        if not await facebook_repository.reject_post(account_id, post_id):
            return ChannelResult([f"Không thể bỏ qua bài #{post_id}."])
        return ChannelResult([f"🗑️ Đã bỏ qua bài Facebook #{post_id}."])

    if command == "/fb_ok":
        parts = raw.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            return ChannelResult(["Cú pháp: /fb_ok <post_id>"])
        post_id = int(parts[1])
        current = await facebook_repository.get_post(account_id, post_id)
        if not current:
            return ChannelResult([f"Không tìm thấy bài #{post_id}."])
        source_urls = find_shopee_urls(current["original_content"])
        cached = await _cached_affiliates(account_id, source_urls)
        missing = [url for url in source_urls if url not in cached]
        if missing:
            return ChannelResult([
                f"⚠️ Bài #{post_id} còn link Shopee chưa có affiliate. Dùng /fb_link {post_id} <affiliate_url> để nhập link trước khi đăng."
            ])
        comment = facebook_caption.build_comment(
            [cached[url] for url in source_urls], seed=post_id,
        )

        page_keys = configured_page_keys()
        if not page_keys:
            return ChannelResult([
                f"❌ Chưa cấu hình Facebook Page nào (FACEBOOK_PAGE_ID/FACEBOOK_PAGE_ACCESS_TOKEN). "
                f"Dùng /fb_pages để kiểm tra. Bài #{post_id} vẫn được giữ nguyên."
            ])

        claimed = await facebook_repository.claim_post(account_id, post_id)
        if not claimed:
            existing_targets = await facebook_repository.list_targets(post_id)
            if existing_targets and all(t["status"] == "POSTED" for t in existing_targets):
                return ChannelResult([f"Bài #{post_id} đã đăng xong trên tất cả page rồi. Dùng /fb_check {post_id} để xem lại."])
            return ChannelResult([f"Bài #{post_id} đã được xử lý hoặc đang đăng."])

        token = claimed["claim_token"]
        try:
            async with facebook_repository.keep_post_claim(post_id, token):
                return await _publish_claimed(
                        account_id, post_id, claimed, page_keys, token, comment,
                    )
        except Exception as exc:
            logger.warning("Facebook queue #%s xử lý gián đoạn (%s).", post_id, type(exc).__name__)
            try:
                await facebook_repository.release_post_claim(account_id, post_id, token, str(exc))
            except Exception:
                logger.warning("Không giải phóng được Facebook claim #%s; lease sẽ phục hồi.", post_id)
            return ChannelResult([
                f"⚠️ Bài #{post_id} bị gián đoạn. Bài đã tạo sẽ không được gửi lại; "
                f"dùng /fb_check {post_id} rồi /fb_ok {post_id} để tiếp tục các page an toàn."
            ])

    if command == "/fb_reconcile":
        parts = raw.split()
        if len(parts) != 4 or not parts[1].isdigit():
            return ChannelResult(["Cú pháp: /fb_reconcile <post_id> <page_key> <facebook_post_id>"])
        post_id, page_key, facebook_id = int(parts[1]), parts[2], parts[3]
        row = await facebook_repository.get_post(account_id, post_id)
        if not row:
            return ChannelResult([f"Không tìm thấy bài #{post_id}."])
        claimed = await facebook_repository.claim_post(account_id, post_id)
        if not claimed:
            return ChannelResult(["Lượt đăng đang chạy hoặc bài đã xử lý; dùng /fb_check để xem trạng thái."])
        token = claimed["claim_token"]
        try:
            async with facebook_repository.keep_post_claim(post_id, token):
                targets = await facebook_repository.list_targets(post_id)
                target = next((t for t in targets if t["page_key"] == page_key), None)
                if not target or target["status"] != "UNKNOWN":
                    return ChannelResult(["Chỉ đối soát target có trạng thái UNKNOWN."])
                status = await inspect_page_post(facebook_id, page_key)
                if status.in_published_posts is not True:
                    return ChannelResult(["Chưa xác nhận Post ID nằm trong published_posts của Page đã chọn."])
                await facebook_repository.record_target_posted(
                    post_id, page_key, facebook_id, status.permalink_url, claim_token=token,
                )
                await facebook_repository.finalize_post_status(
                    account_id, post_id, claim_token=token,
                    needs_comment=bool(find_shopee_urls(row["original_content"])),
                )
        except Exception as exc:
            return ChannelResult([f"Không xác minh được Post ID: {type(exc).__name__}."])
        finally:
            await facebook_repository.release_post_claim(account_id, post_id, token, "Đối soát chưa hoàn tất.")
        return ChannelResult([f"Đã đối soát bài #{post_id}, Page '{page_key}' với Post ID {facebook_id}; không tạo lại."])

    if command == "/fb_check":
        parts = raw.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            return ChannelResult(["Cú pháp: /fb_check <post_id>"])
        post_id = int(parts[1])
        row = await facebook_repository.get_post(account_id, post_id)
        if not row:
            return ChannelResult([f"Không tìm thấy bài #{post_id}."])
        targets = await facebook_repository.list_targets(post_id)
        if not targets:
            return ChannelResult([f"Bài #{post_id} chưa từng /fb_ok nên chưa có page nào để kiểm tra."])
        lines = [f"🔎 FACEBOOK CHECK #{post_id}"]
        for t in targets:
            page_key = t["page_key"]
            if t["status"] != "POSTED" or not (t["facebook_post_id"] or "").strip():
                state = {
                    "PENDING": "chưa đăng", "POSTING": "đang tạo bài",
                    "UNKNOWN": f"chưa rõ kết quả; cần /fb_reconcile {post_id} {page_key} <facebook_post_id>",
                }.get(t["status"], f"lỗi — {t['error_message'] or 'không rõ nguyên nhân'}")
                lines.append(f"— Page '{page_key}': {state}")
                continue
            try:
                status = await inspect_page_post(t["facebook_post_id"], page_key)
            except FacebookPublishError as exc:
                lines.append(f"— Page '{page_key}': không kiểm tra được — {exc}")
                continue
            lines.append(
                f"— Page '{page_key}': Post ID {t['facebook_post_id']} | "
                f"is_published={status.is_published} | is_hidden={status.is_hidden} | "
                f"timeline={status.timeline_visibility or 'không trả về'} | "
                f"in_published_posts={status.in_published_posts}"
            )
            if status.permalink_url:
                lines.append(f"  🔗 {status.permalink_url}")
            if find_shopee_urls(row["original_content"]):
                lines.append(
                    "  💬 Link bình luận: đã thả." if t.get("comment_id")
                    else f"  💬 Link bình luận: CHƯA thả — chạy /fb_ok {post_id} để thử lại."
                )
            lines.append(
                "  ✅ published/public đã xác nhận." if status.public_visibility_confirmed
                else "  ⚠️ chưa xác nhận chắc chắn published/public."
            )
        return ChannelResult(["\n".join(lines)])

    return None


async def _publish_claimed(account_id, post_id, claimed, page_keys, token, comment=""):
    await facebook_repository.ensure_targets(post_id, page_keys)
    targets = {t["page_key"]: t for t in await facebook_repository.list_targets(post_id)}
    to_attempt = [key for key in page_keys if targets[key]["status"] in {"PENDING", "ERROR"}
                  and not targets[key]["facebook_post_id"]]
    lines = [f"📤 Đang xử lý bài #{post_id} trên {len(page_keys)} Facebook Page..."]
    for page_key in page_keys:
        target = targets[page_key]
        if target["facebook_post_id"]:
            lines.append(f"⏭️ Page '{page_key}': đã tạo Post ID {target['facebook_post_id']}, không tạo lại.")
        elif page_key not in to_attempt:
            lines.append(f"⚠️ Page '{page_key}': chưa rõ kết quả lần tạo trước. "
                         f"Kiểm tra Page rồi /fb_reconcile {post_id} {page_key} <facebook_post_id>.")
    media, caption = [], ""
    if to_attempt:
        media_rows = await facebook_repository.get_media(post_id)
        media = [
            await asyncio.to_thread(brand_image, row["mime_type"], bytes(row["content"]))
            for row in media_rows
        ]
        caption = facebook_caption.build_caption(
            claimed["processed_content"], bool(comment), seed=post_id,
        )

    for page_key in to_attempt:
        async def before_create():
            await facebook_repository.mark_target_creating(post_id, page_key, token)

        async def on_created(facebook_post_id):
            await facebook_repository.record_target_posted(
                post_id, page_key, facebook_post_id, None, claim_token=token,
            )

        try:
            published = await publish_page_post(
                caption, media, page_key,
                before_create=before_create, on_created=on_created,
            )
        except FacebookPublicationUncertain as exc:
            if exc.post_id:
                await facebook_repository.record_target_posted(
                    post_id, page_key, exc.post_id, None, claim_token=token,
                )
                lines.append(f"⚠️ Page '{page_key}': đã tạo Post ID {exc.post_id}, cần kiểm tra hiển thị.")
            else:
                await facebook_repository.record_target_unknown(post_id, page_key, str(exc), token)
                lines.append(f"⚠️ Page '{page_key}': chưa rõ đã tạo bài hay chưa; không tự đăng lại. "
                             f"Dùng /fb_reconcile {post_id} {page_key} <facebook_post_id> sau khi kiểm tra Page.")
            continue
        except Exception as exc:
            await facebook_repository.record_target_error(post_id, page_key, str(exc), claim_token=token)
            lines.append(f"❌ Page '{page_key}': chưa tạo được bài — {exc}")
            continue
        await facebook_repository.record_target_posted(
            post_id, page_key, published.post_id, published.permalink_url, claim_token=token,
        )
        detail = f"✅ Page '{page_key}': đã tạo bài. Post ID: {published.post_id}"
        if published.permalink_url:
            detail += f" — {published.permalink_url}"
        if not published.visibility_confirmed:
            detail += f" (⚠️ chưa xác minh published/public, dùng /fb_check {post_id})"
        lines.append(detail)
    if comment:
        await _post_comments(post_id, comment, token, lines)
    overall = await facebook_repository.finalize_post_status(
        account_id, post_id, claim_token=token, needs_comment=bool(comment),
    )
    if overall == "POSTED":
        lines.append(f"Đã tạo bài trên tất cả {len(page_keys)} page; /fb_check {post_id} để xem trạng thái public.")
    else:
        lines.append(f"Bài #{post_id} vẫn được giữ. /fb_ok {post_id} chỉ thử lại page chưa tạo bài an toàn; "
                     "page chưa rõ kết quả cần đối soát trước.")
    return ChannelResult(["\n".join(lines)])


async def _post_comments(post_id, comment, token, lines):
    for target in await facebook_repository.list_targets(post_id):
        page_key = target["page_key"]
        if not target["facebook_post_id"] or target.get("comment_id"):
            continue
        try:
            comment_id = await post_comment(target["facebook_post_id"], comment, page_key)
        except Exception as exc:
            lines.append(
                f"⚠️ Page '{page_key}': bài đã đăng nhưng chưa bình luận được link — {exc}. "
                f"Chạy lại /fb_ok {post_id} để bình luận lại (kiểm tra Page trước nếu lỗi do mạng)."
            )
            continue
        await facebook_repository.record_target_comment(post_id, page_key, comment_id, token)
        lines.append(f"💬 Page '{page_key}': đã thả link vào bình luận đầu tiên.")
