"""Admin commands for the isolated Zalo -> Facebook publishing flow."""

import logging
import os
import re
from typing import Awaitable, Callable
from urllib.parse import urlparse

import asyncpg

from channels import facebook_repository, zalo_repository
from services.channel_result import ChannelResult
from services.facebook_page_service import (
    FacebookPublishError,
    FacebookPublicationUncertain,
    configured_page_keys,
    inspect_page_post,
    publish_page_post,
)

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


def _valid_shopee_affiliate_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return (
        parsed.scheme in _ALLOWED_AFFILIATE_SCHEMES
        and (parsed.hostname or "").lower() == "s.shopee.vn"
    )


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
            if _valid_shopee_affiliate_url(affiliate)
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
                "Tạo short-link trên Shopee Affiliate rồi nhập:",
                f"/fb_link {post_id} <affiliate_url>",
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
        if _valid_shopee_affiliate_url(affiliate)
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


def _replace_caption_links(content, replacements, previous):
    for source, affiliate in replacements.items():
        if source in content:
            content = content.replace(source, affiliate)
        elif affiliate in content:
            continue
        elif previous.get(source) and previous[source] in content and list(previous.values()).count(previous[source]) == 1:
            content = content.replace(previous[source], affiliate)
        else:
            raise ValueError("Caption không còn link nguồn hoặc cache cũ trùng giữa nhiều sản phẩm. "
                             "Dùng /fb_sua <post_id> <caption chứa lại link Shopee nguồn> rồi /fb_link.")
    return content


async def _previous_caption_links(account_id, row, sources):
    if any(source not in row["processed_content"] for source in sources):
        return await facebook_repository.get_previous_affiliate_links(account_id, sources)
    return {}


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
            if _valid_shopee_affiliate_url(affiliate)
        }
        missing = [url for url in source_urls if url not in cached]
        if missing:
            return ChannelResult([
                f"⚠️ Bài #{post_id} còn link Shopee chưa có affiliate. Dùng /fb_link {post_id} <affiliate_url> để nhập link trước khi đăng."
            ])
        # Never rely only on the cache flag: make sure the actual caption being
        # posted contains the cached official Shopee short links.
        previous = await _previous_caption_links(account_id, current, source_urls)
        try:
            content = _replace_caption_links(current["processed_content"], cached, previous)
        except ValueError as exc:
            return ChannelResult([str(exc)])
        if content != current["processed_content"]:
            await facebook_repository.update_content(account_id, post_id, content)

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
                return await _publish_claimed(account_id, post_id, claimed, page_keys, token)
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
                await facebook_repository.finalize_post_status(account_id, post_id, claim_token=token)
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
            lines.append(
                "  ✅ published/public đã xác nhận." if status.public_visibility_confirmed
                else "  ⚠️ chưa xác nhận chắc chắn published/public."
            )
        return ChannelResult(["\n".join(lines)])

    return None


async def _publish_claimed(account_id, post_id, claimed, page_keys, token):
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
    media = []
    if to_attempt:
        media_rows = await facebook_repository.get_media(post_id)
        media = [(row["mime_type"], bytes(row["content"])) for row in media_rows]

    for page_key in to_attempt:
        async def before_create():
            await facebook_repository.mark_target_creating(post_id, page_key, token)

        async def on_created(facebook_post_id):
            await facebook_repository.record_target_posted(
                post_id, page_key, facebook_post_id, None, claim_token=token,
            )

        try:
            published = await publish_page_post(
                claimed["processed_content"], media, page_key,
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
    overall = await facebook_repository.finalize_post_status(account_id, post_id, claim_token=token)
    if overall == "POSTED":
        lines.append(f"Đã tạo bài trên tất cả {len(page_keys)} page; /fb_check {post_id} để xem trạng thái public.")
    else:
        lines.append(f"Bài #{post_id} vẫn được giữ. /fb_ok {post_id} chỉ thử lại page chưa tạo bài an toàn; "
                     "page chưa rõ kết quả cần đối soát trước.")
    return ChannelResult(["\n".join(lines)])
