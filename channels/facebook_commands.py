"""Admin commands for the isolated Zalo -> Facebook publishing flow."""

import os
import re
from urllib.parse import urlparse

import asyncpg

from channels import facebook_repository, zalo_repository
from services.channel_result import ChannelResult
from services.facebook_page_service import FacebookPublishError, publish_page_post

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_SHOPEE_HOSTS = ("shopee.vn", "s.shopee.vn", "shope.ee")
_ALLOWED_AFFILIATE_SCHEMES = {"http", "https"}


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


async def _preview(account_id: str, post_id: int) -> str:
    row = await facebook_repository.get_post(account_id, post_id)
    if not row:
        return f"Không tìm thấy bài #{post_id}."
    media = await facebook_repository.get_media(post_id)
    source_urls = find_shopee_urls(row["original_content"])
    missing = []
    if source_urls:
        cached = await facebook_repository.get_affiliate_links(account_id, source_urls)
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
                "Copy link gốc phía trên, chuyển đổi xong gửi:",
                f"/fb_link {post_id} <link_mới>",
            ]
        )
    lines.extend(["", f"/fb_ok {post_id}  |  /fb_boqua {post_id}"])
    return "\n".join(lines)


async def prepare_post(account_id: str, post_id: int) -> None:
    row = await facebook_repository.get_post(account_id, post_id)
    if not row:
        return
    urls = find_shopee_urls(row["processed_content"])
    cached = await facebook_repository.get_affiliate_links(account_id, urls)
    content = row["processed_content"]
    for source_url, affiliate_url in cached.items():
        content = content.replace(source_url, affiliate_url)
    if content != row["processed_content"]:
        await facebook_repository.update_content(account_id, post_id, content)
    controller = os.getenv("ZALO_CONTROLLER_ID", "").strip()
    if not controller:
        from channels import zalo_session

        controller = await zalo_session.load_controller()
    if controller:
        await zalo_repository.enqueue_outbox(account_id, controller, await _preview(account_id, post_id))


async def maybe_handle_facebook_command(account_id: str, text: str) -> ChannelResult | None:
    raw = text.strip()
    command = raw.split(maxsplit=1)[0].lower() if raw else ""

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
        parts = raw.split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit() or not _valid_http_url(parts[2].strip()):
            return ChannelResult(["Cú pháp: /fb_link <post_id> <affiliate_url>"])
        post_id = int(parts[1])
        row = await facebook_repository.get_post(account_id, post_id)
        if not row or row["status"] not in {"PENDING_APPROVAL", "ERROR"}:
            return ChannelResult([f"Không thể cập nhật link cho bài #{post_id}."])
        urls = find_shopee_urls(row["original_content"])
        if not urls:
            return ChannelResult([f"Bài #{post_id} không có link Shopee cần thay."])
        affiliate_url = parts[2].strip()
        content = row["processed_content"]
        for source_url in urls:
            await facebook_repository.set_affiliate_link(account_id, source_url, affiliate_url)
            content = content.replace(source_url, affiliate_url)
        await facebook_repository.update_content(account_id, post_id, content)
        token = await facebook_repository.create_short_link(affiliate_url)
        base = os.getenv("AFFILIATE_SHORT_BASE_URL", "").strip().rstrip("/")
        suffix = f"{base}/r/{token}" if base else f"/r/{token}"
        return ChannelResult([f"✅ Đã thay link cho bài #{post_id}. Link rút gọn: {suffix}\n\n{await _preview(account_id, post_id)}"])

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
        cached = await facebook_repository.get_affiliate_links(account_id, source_urls)
        missing = [url for url in source_urls if url not in cached]
        if missing:
            return ChannelResult([
                f"⚠️ Bài #{post_id} còn link Shopee chưa có affiliate. Dùng /fb_link {post_id} <affiliate_url> trước khi đăng."
            ])
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
