"""Admin commands for the isolated Zalo -> Facebook publishing flow."""

import asyncio
import logging
import os
import unicodedata
from typing import Awaitable, Callable
from urllib.parse import urlparse

import asyncpg

from channels import (
    facebook_group_names,
    facebook_repository,
    zalo_gateway_status,
    zalo_group_list,
    zalo_repository,
)
from core.envutil import env_on
from services import facebook_caption, facebook_dedup, market_page
from services.channel_result import ChannelResult
from services.facebook_caption import find_shopee_urls
from services.facebook_image import brand_image
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
# Bản kèm nút [✅ Duyệt][🗑 Bỏ qua] (bot_app gửi Telegram có nút, Zoom chữ thường);
# đặt rồi thì preview bài chờ đi đường này thay cho callback chữ.
_admin_buttons_callback: Callable[[str, list], Awaitable[None]] | None = None


def set_admin_notification_callback(
    callback: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Đặt callback chữ; xoá luôn bản kèm nút (đặt lại bằng
    set_admin_buttons_callback SAU lệnh này nếu muốn có nút duyệt)."""
    global _admin_notification_callback, _admin_buttons_callback
    _admin_notification_callback = callback
    _admin_buttons_callback = None


def set_admin_buttons_callback(callback: Callable[[str, list], Awaitable[None]] | None) -> None:
    global _admin_buttons_callback
    _admin_buttons_callback = callback


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
    names = await facebook_group_names.labels(account_id)
    lines = [
        f"📝 BÀI FACEBOOK CHỜ DUYỆT #{post_id}",
        f"Nhóm: {facebook_group_names.label(names, row['group_id'])}",
        f"Người đăng: {row['sender_name'] or row['sender_id']}",
        f"Ảnh: {len(media)}",
    ]
    reposts = _repost_line(row, names)
    if reposts:
        lines.append(reposts)
    lines += [
        "",
        facebook_caption.build_caption(row["processed_content"], bool(source_urls), seed=post_id)
        or "(không có caption)",
    ]
    if cached:
        lines.extend(
            [
                "",
                "💬 Bình luận đầu tiên:",
                facebook_caption.build_comment(
                    [cached[url] for url in source_urls if url in cached],
                    seed=post_id,
                ),
            ]
        )
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


def _row_get(row, key: str, default=None):
    getter = getattr(row, "get", None)
    value = getter(key) if getter is not None else None
    return default if value is None else value


def _repost_line(row, names: dict[str, str]) -> str:
    """ "🔁 Đã chặn 3 lần đăng lại, ở nhóm: A, B" - bài trùng tới từ các nhóm nguồn khác."""
    count = int(_row_get(row, "dup_count", 0) or 0)
    if count <= 0:
        return ""
    groups = facebook_group_names.labels_text(names, _row_get(row, "dup_groups", []) or [])
    where = f", ở nhóm: {groups}" if groups else ""
    return f"🔁 Đã chặn {count} lần đăng lại{where}"


async def _held_in_quiet_hours_owner(preview: str) -> bool:
    """Bài chờ duyệt là tin URGENT (services/notify_hub): ngoài giờ yên lặng gửi ngay
    (kèm nút); trong giờ yên lặng của chủ bot thì giữ trong hàng đợi, gửi đầu tiên
    khi hết giờ - bản giữ lại chỉ về Telegram, không kèm nút."""
    from core import config
    from services import notify_hub

    owner = config.ALLOWED_USER_ID
    return bool(owner) and await notify_hub.offer(
        "telegram", owner, preview, source="facebook_review"
    )


async def _held_in_quiet_hours_zalo(controller: str, preview: str) -> bool:
    """Như trên cho bản gửi Zalo controller (giờ yên lặng của chính Zalo đó)."""
    from channels import zalo_users
    from services import notify_hub

    if not notify_hub.active():
        return False
    try:
        user = await zalo_users.resolve(controller)
    except Exception:
        logger.warning("Không đọc được Zalo controller để xét giờ yên lặng.", exc_info=True)
        return False
    if user is None or not user.is_active:
        return False
    return await notify_hub.offer("zalo", user.internal_user_id, preview, source="facebook_review")


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
    if controller and not await _held_in_quiet_hours_zalo(controller, preview):
        await zalo_repository.enqueue_outbox(account_id, controller, preview)
    if await _held_in_quiet_hours_owner(preview):
        return  # giữ tới hết giờ yên lặng (tin vẫn có dòng /fb_ok, /fb_boqua)
    try:
        if _admin_buttons_callback is not None:
            from services import action_buttons

            await _admin_buttons_callback(preview, action_buttons.facebook_review_rows(post_id))
        elif _admin_notification_callback is not None:
            await _admin_notification_callback(preview)
    except Exception:
        logger.warning("Không gửi được preview Facebook tới kênh admin phụ.", exc_info=True)


# /fb_reset xoá TOÀN BỘ bài đã lưu, không hoàn tác: lệnh nằm trong menu Telegram
# nên 1 lần chạm nhầm từng xoá sạch hàng chờ - giờ phải gõ kèm từ xác nhận.
FB_RESET_CONFIRM_WORD = "xacnhan"


def _is_reset_confirmed(argument: str) -> bool:
    """Nhận "xacnhan", "xác nhận", "XACNHAN"... (bỏ dấu, khoảng trắng, hoa thường)."""
    folded = unicodedata.normalize("NFD", argument.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch) and not ch.isspace())
    return folded == FB_RESET_CONFIRM_WORD


async def _fb_reset_warning(account_id: str) -> str:
    try:
        pending = f" (đang có {await facebook_repository.count_pending(account_id)} bài chờ duyệt)"
    except Exception:
        logger.warning("Không đếm được bài Facebook chờ duyệt.", exc_info=True)
        pending = ""
    return (
        f"⚠️ /fb_reset sẽ XOÁ TOÀN BỘ bài Facebook đã lưu của tài khoản này{pending}: bài chờ "
        "duyệt, lịch sử đăng và dấu chống trùng - không hoàn tác được.\n"
        f"Gõ /fb_reset {FB_RESET_CONFIRM_WORD} nếu anh chắc chắn muốn xoá."
    )


def _row_fingerprint(row) -> "facebook_dedup.Fingerprint":
    """Dấu vân tay đã lưu của bài chờ; bài cũ chưa có thì dựng lại từ chữ gốc."""
    group_id = _row_get(row, "group_id", "")
    sender_id = _row_get(row, "sender_id", "")
    folded = _row_get(row, "folded_text")
    if folded is None:
        return facebook_dedup.build_fingerprint(
            row["original_content"] or "", group_id=group_id, sender_id=sender_id
        )
    return facebook_dedup.Fingerprint(
        text_hash=_row_get(row, "text_hash"),
        product_keys=list(_row_get(row, "product_keys", []) or []),
        image_hashes=list(_row_get(row, "image_hashes", []) or []),
        folded_text=folded,
        group_id=group_id,
        sender_id=sender_id,
    )


async def _pending_duplicates(account_id: str, rows: list) -> list:
    """[(bài chờ, DuplicateMatch)] - bài chờ trùng một bài CŨ HƠN (id nhỏ hơn) trong cửa
    sổ chống trùng, ở bất kỳ nhóm nguồn nào. Như lúc nhận bài: trùng chính xác nội dung/
    sản phẩm tra trên cả cửa sổ ngày, so gần giống với FACEBOOK_DEDUP_SCAN_LIMIT bài gần
    nhất. So trong thread (CPU thuần Python)."""
    if not rows:
        return []
    candidates = [(row, _row_fingerprint(row)) for row in rows]
    text_hashes = sorted({fp.text_hash for _, fp in candidates if fp.text_hash})
    product_keys = sorted({key for _, fp in candidates for key in fp.product_keys})
    try:
        exact = await facebook_repository.exact_fingerprints(account_id, text_hashes, product_keys)
        recent = await facebook_repository.recent_fingerprints(account_id)
    except Exception:
        logger.warning("Không đọc được dấu vân tay để lọc trùng hàng chờ.", exc_info=True)
        return []
    seen = {int(row["post_id"]) for row in exact}
    fingerprints = [*exact, *(row for row in recent if int(row["post_id"]) not in seen)]

    def scan() -> list:
        index = facebook_dedup.DedupIndex(fingerprints)
        found = []
        for row, fingerprint in sorted(candidates, key=lambda item: int(item[0]["id"])):
            match = index.find(fingerprint, before_post_id=int(row["id"]))
            if match is not None:
                found.append((row, match))
        return found

    return await asyncio.to_thread(scan)


async def _facebook_sources(account_id: str) -> dict[str, str]:
    """group_id -> tên gợi nhớ của các nhóm nguồn Facebook."""
    return dict(await facebook_repository.list_groups(account_id))


async def maybe_handle_facebook_command(account_id: str, text: str) -> ChannelResult | None:
    raw = text.strip()
    command = raw.split(maxsplit=1)[0].lower() if raw else ""

    if command == "/fb_reset":
        argument = raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) == 2 else ""
        if not _is_reset_confirmed(argument):
            return ChannelResult([await _fb_reset_warning(account_id)])
        deleted, sequence_reset = await facebook_repository.reset_posts(account_id)
        if sequence_reset:
            return ChannelResult(
                [
                    f"✅ Đã xóa {deleted} bài Facebook đã lưu. Bài mới tiếp theo sẽ bắt đầu lại từ #1."
                ]
            )
        return ChannelResult(
            [
                f"✅ Đã xóa {deleted} bài Facebook của tài khoản này. "
                "ID chưa thể về #1 vì vẫn còn bài của tài khoản Facebook/Zalo khác trong hàng đợi."
            ]
        )

    if command == "/fb_loclai":
        # Chạy lại bộ lọc hiện tại cho các bài đang chờ (bài vào hàng chờ trước khi bộ
        # lọc được cải thiện), rồi lọc trùng GIỮA CÁC NHÓM: bài sau trùng bài trước (kể
        # cả bài đã đăng/bỏ qua) thì bỏ. Bài bị lọc chuyển sang "bỏ qua" và xoá ảnh.
        rows = await facebook_repository.list_pending_for_refilter(account_id)
        removed: list[tuple[int, str]] = []
        survivors = []
        for row in rows:
            reason = facebook_caption.skip_reason(
                row["original_content"] or "",
                int(row["media_count"] or 0) > 0,
                require_photo_and_caption=env_on("FACEBOOK_REQUIRE_PHOTO_AND_CAPTION"),
                skip_voucher=env_on("FACEBOOK_SKIP_VOUCHER_POSTS"),
                skip_non_sale=env_on("FACEBOOK_SKIP_NON_SALE_POSTS"),
            )
            if not reason:
                survivors.append(row)
            elif await facebook_repository.reject_post(account_id, int(row["id"])):
                removed.append((int(row["id"]), reason))
        duplicates = await _pending_duplicates(account_id, survivors)
        names = await facebook_group_names.labels(account_id) if duplicates else {}
        for row, match in duplicates:
            post_id = int(row["id"])
            if not await facebook_repository.reject_post(account_id, post_id):
                continue
            origin = (
                f" ở nhóm {facebook_group_names.label(names, match.group_id)}"
                if match.cross_group
                else ""
            )
            removed.append((post_id, f"trùng bài #{match.post_id}{origin} ({match.reason})"))
            try:
                await facebook_repository.record_duplicate(
                    match.post_id, _row_get(row, "group_id", "")
                )
            except Exception:
                logger.warning("Không ghi được lần đăng lại của bài #%s.", match.post_id)
        if not removed:
            return ChannelResult([f"✅ Đã kiểm tra {len(rows)} bài chờ: không có bài nào cần bỏ."])
        removed.sort()
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
        return ChannelResult(
            [
                facebook_intake_stats.summary()
                + f"\n\n📥 Đang chờ duyệt: {pending}/{facebook_repository.max_pending()} bài."
            ]
        )

    if command == "/fb_nhom":
        groups = await facebook_group_names.sources(account_id)
        if not groups:
            return ChannelResult(
                [
                    "Chưa có nhóm nguồn Facebook. Gõ /nhomzalo để xem nhóm rồi "
                    "/fb_themnhom <số> [tên-gợi-nhớ]."
                ]
            )
        lines = [f"📣 Nhóm nguồn đăng Facebook ({len(groups)}):"]
        lines.extend(
            f"{i}. {group.name or facebook_group_names.UNKNOWN} · gợi nhớ: {group.alias}"
            for i, group in enumerate(groups, 1)
        )
        lines.append(
            "🔁 Bài trùng được lọc chung cho mọi nhóm: một deal đăng ở nhiều nhóm chỉ vào "
            "hàng chờ 1 lần."
        )
        lines.append("Bỏ nhóm: /fb_xoanhom <tên gợi nhớ hoặc tên nhóm>")
        return ChannelResult(["\n".join(lines)])

    if command == "/fb_pages":
        pages = configured_page_keys()
        if not pages:
            return ChannelResult(
                [
                    "Chưa cấu hình Facebook Page nào. Đặt FACEBOOK_PAGE_ID + FACEBOOK_PAGE_ACCESS_TOKEN "
                    "cho page mặc định, hoặc FACEBOOK_PAGE_ID_<key> + FACEBOOK_PAGE_ACCESS_TOKEN_<key> "
                    "cho page bổ sung (ví dụ key=2)."
                ]
            )
        lines = [
            "📄 Facebook Page đã cấu hình — /fb_ok sẽ đăng lên TẤT CẢ các page này:",
        ]
        lines.extend(f"- {key}" for key in pages)
        return ChannelResult(["\n".join(lines)])

    if command == "/fb_market":
        parts = raw.lower().split()
        if len(parts) < 2 or parts[1] not in ("stock", "news") or parts[2:] not in ([], ["dang"]):
            return ChannelResult(
                [
                    "Cú pháp: /fb_market <stock|news> [dang]\n"
                    "- stock: báo cáo VN-INDEX; news: tin CafeF\n"
                    "- không có 'dang': chỉ tạo nội dung để xem thử, KHÔNG đăng\n"
                    "- thêm 'dang': đăng thật lên Page chứng khoán (FACEBOOK_PAGE_ID_MARKET)"
                ]
            )
        job, publish = parts[1], parts[2:] == ["dang"]
        try:
            market_text = await market_page.run_manual(job, publish=publish)
        except market_page.MarketPageError as exc:
            return ChannelResult([f"❌ {exc}"])
        except FacebookPublicationUncertain:
            return ChannelResult(
                [
                    "⚠️ Chưa rõ Facebook đã tạo bài hay chưa. Kiểm tra Page chứng khoán trước khi chạy lại."
                ]
            )
        except Exception as exc:
            logger.warning("/fb_market %s lỗi (%s).", job, type(exc).__name__, exc_info=True)
            return ChannelResult([f"❌ Chạy {job} thất bại: {type(exc).__name__}: {exc}"])
        if not market_text:
            return ChannelResult([f"Không có nội dung để đăng cho {job} (xem log để biết lý do)."])
        if publish:
            return ChannelResult([f"✅ Đã đăng {job} lên Page chứng khoán:\n\n{market_text}"])
        return ChannelResult(
            [f"👀 XEM THỬ {job} (chưa đăng):\n\n{market_text}\n\nĐăng thật: /fb_market {job} dang"]
        )

    if command == "/fb_themnhom":
        parts = raw.split(maxsplit=2)
        if len(parts) < 2:
            return ChannelResult(
                [
                    "Cú pháp: /fb_themnhom <số|group_id> [tên-gợi-nhớ] — số thứ tự lấy từ "
                    "/nhomzalo (danh sách nhóm bot đang ở)."
                ]
            )
        try:
            group_id, name, alias = await zalo_group_list.resolve_add(
                account_id,
                parts[1:],
                lambda: _facebook_sources(account_id),
                "Nhóm này đã là nguồn Facebook với tên gợi nhớ “{alias}”.",
            )
        except zalo_group_list.PickError as exc:
            return ChannelResult([str(exc)])
        if not group_id or not alias or len(alias) > 100:
            return ChannelResult(["Group ID hoặc tên gợi nhớ không hợp lệ."])
        try:
            await facebook_repository.add_group(account_id, group_id, alias, name)
        except asyncpg.UniqueViolationError:
            return ChannelResult([f"Tên gợi nhớ “{alias}” đang được dùng cho nhóm Facebook khác."])
        facebook_group_names.invalidate(account_id)
        await zalo_gateway_status.refresh_groups()
        if not name:
            # Thêm bằng group ID: hỏi gateway tên thật của nhóm để hiện thay cho ID.
            name = await facebook_group_names.name_of(account_id, group_id)
        shown = f"“{name}” (gợi nhớ: {alias})" if name else f"“{alias}”"
        return ChannelResult(
            [
                f"✅ Đã thêm nhóm Facebook {shown}. Bài trùng được lọc chung với mọi nhóm "
                "nguồn khác. Không ảnh hưởng /tongket."
            ]
        )

    if command == "/fb_xoanhom":
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            return ChannelResult(["Cú pháp: /fb_xoanhom <tên gợi nhớ hoặc tên nhóm>"])
        target = parts[1].strip()
        groups = await facebook_group_names.sources(account_id, fill_missing=False)
        group = facebook_group_names.find(groups, target)
        group_id = group.group_id if group is not None else target
        if not await facebook_repository.remove_group(account_id, group_id):
            return ChannelResult(
                [f"Không tìm thấy nhóm Facebook “{target}”. Gõ /fb_nhom để xem tên nhóm."]
            )
        facebook_group_names.invalidate(account_id)
        await zalo_gateway_status.refresh_groups()
        shown = facebook_group_names.label_of(group) if group is not None else target
        return ChannelResult(
            [f"✅ Đã xóa nhóm Facebook “{shown}”. Danh sách /tongket không thay đổi."]
        )

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
            return ChannelResult(
                [
                    "Cú pháp: /fb_link <post_id> <affiliate_url> (bài có 1 link Shopee)\n"
                    "hoặc: /fb_link <post_id> <source_url> <affiliate_url> (bài có nhiều link)\n"
                    "Link affiliate phải là short-link dạng https://s.shopee.vn/..."
                ]
            )
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
                return ChannelResult(
                    [
                        f"Bài #{post_id} có {len(urls)} link Shopee. "
                        f"Nhập từng link: /fb_link {post_id} <source_url> <affiliate_url>."
                    ]
                )
            source_url, affiliate_url = urls[0], parts[2]
        else:
            source_url, affiliate_url = parts[2], parts[3]
            if source_url not in urls:
                return ChannelResult(["source_url không thuộc bài Facebook này."])

        if not _valid_shopee_affiliate_url(affiliate_url):
            return ChannelResult(
                ["Link affiliate phải là short-link chính thức dạng https://s.shopee.vn/..."]
            )
        await facebook_repository.set_affiliate_link(account_id, source_url, affiliate_url)
        note = await _auto_rewrite(account_id, row)
        saved = f"✅ Đã lưu short affiliate link chính thức cho bài #{post_id}: {affiliate_url}"
        return ChannelResult(
            ["\n".join(filter(None, [saved, note])) + f"\n\n{await _preview(account_id, post_id)}"]
        )

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
            return ChannelResult(
                [
                    f"⚠️ Bài #{post_id} còn link Shopee chưa có affiliate. Dùng /fb_link {post_id} <affiliate_url> để nhập link trước khi đăng."
                ]
            )
        comment = facebook_caption.build_comment(
            [cached[url] for url in source_urls],
            seed=post_id,
        )

        page_keys = configured_page_keys()
        if not page_keys:
            return ChannelResult(
                [
                    f"❌ Chưa cấu hình Facebook Page nào (FACEBOOK_PAGE_ID/FACEBOOK_PAGE_ACCESS_TOKEN). "
                    f"Dùng /fb_pages để kiểm tra. Bài #{post_id} vẫn được giữ nguyên."
                ]
            )

        claimed = await facebook_repository.claim_post(account_id, post_id)
        if not claimed:
            existing_targets = await facebook_repository.list_targets(post_id)
            if existing_targets and all(t["status"] == "POSTED" for t in existing_targets):
                return ChannelResult(
                    [
                        f"Bài #{post_id} đã đăng xong trên tất cả page rồi. Dùng /fb_check {post_id} để xem lại."
                    ]
                )
            return ChannelResult([f"Bài #{post_id} đã được xử lý hoặc đang đăng."])

        token = claimed["claim_token"]
        try:
            async with facebook_repository.keep_post_claim(post_id, token):
                return await _publish_claimed(
                    account_id,
                    post_id,
                    claimed,
                    page_keys,
                    token,
                    comment,
                )
        except Exception as exc:
            logger.warning("Facebook queue #%s xử lý gián đoạn (%s).", post_id, type(exc).__name__)
            try:
                await facebook_repository.release_post_claim(account_id, post_id, token, str(exc))
            except Exception:
                logger.warning(
                    "Không giải phóng được Facebook claim #%s; lease sẽ phục hồi.", post_id
                )
            return ChannelResult(
                [
                    f"⚠️ Bài #{post_id} bị gián đoạn. Bài đã tạo sẽ không được gửi lại; "
                    f"dùng /fb_check {post_id} rồi /fb_ok {post_id} để tiếp tục các page an toàn."
                ]
            )

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
            return ChannelResult(
                ["Lượt đăng đang chạy hoặc bài đã xử lý; dùng /fb_check để xem trạng thái."]
            )
        token = claimed["claim_token"]
        try:
            async with facebook_repository.keep_post_claim(post_id, token):
                targets = await facebook_repository.list_targets(post_id)
                target = next((t for t in targets if t["page_key"] == page_key), None)
                if not target or target["status"] != "UNKNOWN":
                    return ChannelResult(["Chỉ đối soát target có trạng thái UNKNOWN."])
                status = await inspect_page_post(facebook_id, page_key)
                if status.in_published_posts is not True:
                    return ChannelResult(
                        ["Chưa xác nhận Post ID nằm trong published_posts của Page đã chọn."]
                    )
                await facebook_repository.record_target_posted(
                    post_id,
                    page_key,
                    facebook_id,
                    status.permalink_url,
                    claim_token=token,
                )
                await facebook_repository.finalize_post_status(
                    account_id,
                    post_id,
                    claim_token=token,
                    needs_comment=bool(find_shopee_urls(row["original_content"])),
                )
        except Exception as exc:
            return ChannelResult([f"Không xác minh được Post ID: {type(exc).__name__}."])
        finally:
            await facebook_repository.release_post_claim(
                account_id, post_id, token, "Đối soát chưa hoàn tất."
            )
        return ChannelResult(
            [
                f"Đã đối soát bài #{post_id}, Page '{page_key}' với Post ID {facebook_id}; không tạo lại."
            ]
        )

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
            return ChannelResult(
                [f"Bài #{post_id} chưa từng /fb_ok nên chưa có page nào để kiểm tra."]
            )
        lines = [f"🔎 FACEBOOK CHECK #{post_id}"]
        for t in targets:
            page_key = t["page_key"]
            if t["status"] != "POSTED" or not (t["facebook_post_id"] or "").strip():
                state = {
                    "PENDING": "chưa đăng",
                    "POSTING": "đang tạo bài",
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
                    "  💬 Link bình luận: đã thả."
                    if t.get("comment_id")
                    else f"  💬 Link bình luận: CHƯA thả — chạy /fb_ok {post_id} để thử lại."
                )
            lines.append(
                "  ✅ published/public đã xác nhận."
                if status.public_visibility_confirmed
                else "  ⚠️ chưa xác nhận chắc chắn published/public."
            )
        return ChannelResult(["\n".join(lines)])

    return None


async def _publish_claimed(account_id, post_id, claimed, page_keys, token, comment=""):
    await facebook_repository.ensure_targets(post_id, page_keys)
    targets = {t["page_key"]: t for t in await facebook_repository.list_targets(post_id)}
    to_attempt = [
        key
        for key in page_keys
        if targets[key]["status"] in {"PENDING", "ERROR"} and not targets[key]["facebook_post_id"]
    ]
    lines = [f"📤 Đang xử lý bài #{post_id} trên {len(page_keys)} Facebook Page..."]
    for page_key in page_keys:
        target = targets[page_key]
        if target["facebook_post_id"]:
            lines.append(
                f"⏭️ Page '{page_key}': đã tạo Post ID {target['facebook_post_id']}, không tạo lại."
            )
        elif page_key not in to_attempt:
            lines.append(
                f"⚠️ Page '{page_key}': chưa rõ kết quả lần tạo trước. "
                f"Kiểm tra Page rồi /fb_reconcile {post_id} {page_key} <facebook_post_id>."
            )
    media, caption = [], ""
    if to_attempt:
        media_rows = await facebook_repository.get_media(post_id)
        media = [
            await asyncio.to_thread(brand_image, row["mime_type"], bytes(row["content"]))
            for row in media_rows
        ]
        caption = facebook_caption.build_caption(
            claimed["processed_content"],
            bool(comment),
            seed=post_id,
        )

    for page_key in to_attempt:

        async def before_create(page_key=page_key):
            await facebook_repository.mark_target_creating(post_id, page_key, token)

        async def on_created(facebook_post_id, page_key=page_key):
            await facebook_repository.record_target_posted(
                post_id,
                page_key,
                facebook_post_id,
                None,
                claim_token=token,
            )

        try:
            published = await publish_page_post(
                caption,
                media,
                page_key,
                before_create=before_create,
                on_created=on_created,
            )
        except FacebookPublicationUncertain as exc:
            if exc.post_id:
                await facebook_repository.record_target_posted(
                    post_id,
                    page_key,
                    exc.post_id,
                    None,
                    claim_token=token,
                )
                lines.append(
                    f"⚠️ Page '{page_key}': đã tạo Post ID {exc.post_id}, cần kiểm tra hiển thị."
                )
            else:
                await facebook_repository.record_target_unknown(post_id, page_key, str(exc), token)
                lines.append(
                    f"⚠️ Page '{page_key}': chưa rõ đã tạo bài hay chưa; không tự đăng lại. "
                    f"Dùng /fb_reconcile {post_id} {page_key} <facebook_post_id> sau khi kiểm tra Page."
                )
            continue
        except Exception as exc:
            await facebook_repository.record_target_error(
                post_id, page_key, str(exc), claim_token=token
            )
            lines.append(f"❌ Page '{page_key}': chưa tạo được bài — {exc}")
            continue
        await facebook_repository.record_target_posted(
            post_id,
            page_key,
            published.post_id,
            published.permalink_url,
            claim_token=token,
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
        account_id,
        post_id,
        claim_token=token,
        needs_comment=bool(comment),
    )
    if overall == "POSTED":
        lines.append(
            f"Đã tạo bài trên tất cả {len(page_keys)} page; /fb_check {post_id} để xem trạng thái public."
        )
    else:
        lines.append(
            f"Bài #{post_id} vẫn được giữ. /fb_ok {post_id} chỉ thử lại page chưa tạo bài an toàn; "
            "page chưa rõ kết quả cần đối soát trước."
        )
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
