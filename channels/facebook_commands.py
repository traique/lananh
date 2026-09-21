"""Admin commands for the isolated Zalo -> Facebook publishing flow."""

import logging
import os
import re
from typing import Awaitable, Callable
from urllib.parse import urlparse

import asyncpg

from channels import facebook_repository, zalo_repository
from core import config
from services.channel_result import ChannelResult
from services.facebook_page_service import FacebookPublishError, publish_page_post
from services import shopee_affiliate_browser

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_SHOPEE_HOSTS = ("shopee.vn", "s.shopee.vn", "shope.ee")
_ALLOWED_AFFILIATE_SCHEMES = {"http", "https"}
logger = logging.getLogger(__name__)
_admin_notification_callback: Callable[[str], Awaitable[None]] | None = None


def set_admin_notification_callback(
    callback: Callable[[str], Awaitable[None]] | None,
) -> None:
    global _admin_notification_callback
    _admin_notification_callback = callback


def find_shopee_urls(text: str) -> list[str]:
    urls: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,);]}")
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            continue
        if any(host == domain or host.endswith(f".{domain}") for domain in _SHOPEE_HOSTS):
            urls.append(url)
    return list(dict.fromkeys(urls))


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in _ALLOWED_AFFILIATE_SCHEMES and bool(parsed.netloc)


def _valid_shopee_affiliate_url(value: str) -> bool:
    if not _valid_http_url(value):
        return False
    try:
        return (urlparse(value).hostname or "").lower() == "s.shopee.vn"
    except ValueError:
        return False


async def _preview(account_id: str, post_id: int) -> str:
    row = await facebook_repository.get_post(account_id, post_id)
    if not row:
        return f"Không tìm thấy bài #{post_id}."
    media = await facebook_repository.get_media(post_id)
    source_urls = find_shopee_urls(row["original_content"])
    missing = []
    if source_urls:
        cached = {
            source: affiliate
            for source, affiliate in (
                await facebook_repository.get_affiliate_links(account_id, source_urls)
            ).items()
            if shopee_affiliate_browser.is_official_short_affiliate_url(affiliate)
        }
        missing = [url for url in source_urls if url not in cached]
    lines = [
        f"📝 BÀI FACEBOOK CHỜ DUYỆT #{post_id}",
        f"Nhóm: {row['group_id']}",
        f"Người đăng: {row['sender_name'] or row['sender_id']}",
        f"Ảnh: {len(media)}",
        "",
        row["processed_content"] or "(không có caption)",
    ]
    if source_urls:
        lines.extend(["", "🔗 Link Shopee gốc (chưa chuyển đổi):", *source_urls])
    if missing:
        lines.extend(
            [
                "",
                "⚠️ Có link Shopee chưa được đổi sang affiliate của bạn.",
                "Tự động chuyển bằng Shopee Affiliate:",
                f"/fb_link {post_id}",
                "Nếu Shopee yêu cầu đăng nhập/CAPTCHA, vẫn có thể nhập link thủ công.",
            ]
        )
    lines.extend(["", f"/fb_ok {post_id}  |  /fb_boqua {post_id}"])
    return "\n".join(lines)


async def prepare_post(account_id: str, post_id: int) -> None:
    row = await facebook_repository.get_post(account_id, post_id)
    if not row:
        return
    urls = find_shopee_urls(row["processed_content"])
    cached = {
        source: affiliate
        for source, affiliate in (
            await facebook_repository.get_affiliate_links(account_id, urls)
        ).items()
        if shopee_affiliate_browser.is_official_short_affiliate_url(affiliate)
    }
    content = row["processed_content"]
    for source_url, affiliate_url in cached.items():
        content = content.replace(source_url, affiliate_url)
    if content != row["processed_content"]:
        await facebook_repository.update_content(account_id, post_id, content)
    controller = os.getenv("ZALO_CONTROLLER_ID", "").strip()
    if not controller:
        from channels import zalo_session

        controller = await zalo_session.load_controller()
    preview = await _preview(account_id, post_id)
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

    if command == "/fb_nhom":
        groups = await facebook_repository.list_groups(account_id)
        if not groups:
            return ChannelResult(["Chưa có nhóm nguồn Facebook. Dùng /fb_themnhom <group_id> <tên-gợi-nhớ>."])
        lines = ["📣 Nhóm nguồn đăng Facebook:"]
        lines.extend(f"{i}. {alias} — {group_id}" for i, (group_id, alias) in enumerate(groups, 1))
        return ChannelResult(["\n".join(lines)])

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
        if len(parts) < 2 or not parts[1].isdigit():
            return ChannelResult([
                "Cú pháp: /fb_link <post_id> (tự động)\n"
                "Fallback 1 link: /fb_link <post_id> <affiliate_url>\n"
                "Fallback nhiều link: /fb_link <post_id> <source_url> <affiliate_url>"
            ])
        post_id = int(parts[1])
        row = await facebook_repository.get_post(account_id, post_id)
        if not row or row["status"] not in {"PENDING_APPROVAL", "ERROR"}:
            return ChannelResult([f"Không thể cập nhật link cho bài #{post_id}."])
        urls = find_shopee_urls(row["original_content"])
        if not urls:
            return ChannelResult([f"Bài #{post_id} không có link Shopee cần thay."])

        # /fb_link <id>: official Custom Link page through a short-lived browser.
        if len(parts) == 2:
            if not config.SHOPEE_AFFILIATE_AUTO_ENABLED:
                return ChannelResult([
                    "Tự động chuyển Shopee Affiliate đang tắt. "
                    f"Dùng /fb_link {post_id} <affiliate_url> để nhập thủ công."
                ])
            try:
                conversions = await shopee_affiliate_browser.convert_urls(account_id, urls)
            except shopee_affiliate_browser.ShopeeAffiliateError as exc:
                return ChannelResult([
                    f"⚠️ Chưa tự chuyển được link cho bài #{post_id}: {exc}\n"
                    "Bài vẫn được giữ nguyên và chưa đăng. "
                    "Có thể nạp lại session trong /admin hoặc dùng fallback thủ công."
                ])
            content = row["processed_content"]
            for conversion in conversions:
                content = content.replace(conversion.source_url, conversion.affiliate_url)
            await facebook_repository.update_content(account_id, post_id, content)
            generated = sum(not item.from_cache for item in conversions)
            cached = len(conversions) - generated
            links = "\n".join(
                f"{idx}. {item.affiliate_url}" for idx, item in enumerate(conversions, 1)
            )
            detail = []
            if generated:
                detail.append(f"{generated} link mới")
            if cached:
                detail.append(f"{cached} link từ cache")
            summary = ", ".join(detail) or "đã xử lý"
            return ChannelResult([
                f"✅ Đã chuyển affiliate Shopee cho bài #{post_id} ({summary}).\n"
                f"{links}\n\n{await _preview(account_id, post_id)}"
            ])

        # Manual fallback. With >1 source URL, require explicit source mapping so
        # one product's affiliate link can never overwrite all products in a post.
        if len(parts) == 3:
            if len(urls) != 1:
                return ChannelResult([
                    f"Bài #{post_id} có {len(urls)} link Shopee. Dùng tự động /fb_link {post_id}, "
                    "hoặc nhập từng link: /fb_link <post_id> <source_url> <affiliate_url>."
                ])
            source_url, affiliate_url = urls[0], parts[2]
        elif len(parts) == 4:
            source_url, affiliate_url = parts[2], parts[3]
            if source_url not in urls:
                return ChannelResult(["source_url không thuộc bài Facebook này."])
        else:
            return ChannelResult([
                "Cú pháp fallback: /fb_link <post_id> <affiliate_url> hoặc "
                "/fb_link <post_id> <source_url> <affiliate_url>"
            ])

        if not _valid_shopee_affiliate_url(affiliate_url):
            return ChannelResult([
                "Link affiliate thủ công phải là short-link chính thức dạng https://s.shopee.vn/..."
            ])
        canonical_key = None
        try:
            resolved = await shopee_affiliate_browser.resolve_shopee_url(source_url)
            canonical_key = resolved.canonical_key
        except shopee_affiliate_browser.ShopeeAffiliateError:
            # Manual mode is the emergency fallback; a resolver outage must not block it.
            pass
        await facebook_repository.set_affiliate_link(
            account_id, source_url, affiliate_url, canonical_key=canonical_key
        )
        content = row["processed_content"].replace(source_url, affiliate_url)
        await facebook_repository.update_content(account_id, post_id, content)
        return ChannelResult([
            f"✅ Đã lưu short affiliate link chính thức cho bài #{post_id}: {affiliate_url}\n\n"
            f"{await _preview(account_id, post_id)}"
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
        cached = {
            source: affiliate
            for source, affiliate in (
                await facebook_repository.get_affiliate_links(account_id, source_urls)
            ).items()
            if shopee_affiliate_browser.is_official_short_affiliate_url(affiliate)
        }
        missing = [url for url in source_urls if url not in cached]
        if missing:
            return ChannelResult([
                f"⚠️ Bài #{post_id} còn link Shopee chưa có affiliate. Dùng /fb_link {post_id} để tự chuyển trước khi đăng."
            ])
        # Never rely only on the cache flag: make sure the actual caption being
        # posted contains the cached official Shopee short links.
        content = current["processed_content"]
        for source_url, affiliate_url in cached.items():
            content = content.replace(source_url, affiliate_url)
        if content != current["processed_content"]:
            await facebook_repository.update_content(account_id, post_id, content)
        claimed = await facebook_repository.claim_post(account_id, post_id)
        if not claimed:
            return ChannelResult([f"Bài #{post_id} đã được xử lý hoặc đang đăng."])
        media_rows = await facebook_repository.get_media(post_id)
        media = [(row["mime_type"], bytes(row["content"])) for row in media_rows]
        try:
            facebook_post_id = await publish_page_post(claimed["processed_content"], media)
        except FacebookPublishError as exc:
            await facebook_repository.mark_error(account_id, post_id, str(exc))
            return ChannelResult([f"❌ Đăng Facebook thất bại cho bài #{post_id}: {exc}"])
        except Exception as exc:
            await facebook_repository.mark_error(account_id, post_id, str(exc))
            return ChannelResult([f"❌ Đăng Facebook thất bại cho bài #{post_id}: {exc}"])
        await facebook_repository.mark_posted(account_id, post_id, facebook_post_id)
        return ChannelResult([f"✅ Đã đăng bài #{post_id} lên Facebook. Post ID: {facebook_post_id}"])

    return None
