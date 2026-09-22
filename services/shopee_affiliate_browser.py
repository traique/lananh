"""Shopee Affiliate Custom Link automation, optimized for small Render instances.

No private Shopee API is used. A short affiliate URL is produced through the
same official Custom Link page a logged-in user uses in a browser. Chromium is
started only for cache misses, conversions are serialized globally, heavy
resources are blocked, and the browser is closed immediately after the batch.

The module deliberately does not attempt to bypass CAPTCHA/verification. If
Shopee asks for login or human verification, conversion fails closed and the
existing manual /fb_link fallback remains available.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from channels import facebook_repository, shopee_affiliate_session
from core import config
from services.web_reader import WebReaderError, normalize_public_http_url

logger = logging.getLogger(__name__)

_SHOPEE_HOSTS = ("shopee.vn", "s.shopee.vn", "shope.ee")
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 6
_AFFILIATE_URL_RE = re.compile(r"https://s\.shopee\.vn/[A-Za-z0-9_-]+", re.IGNORECASE)
_PRODUCT_PATH_RE = re.compile(r"/product/(\d+)/(\d+)(?:/|$)", re.IGNORECASE)
_PRODUCT_SLUG_RE = re.compile(r"-i\.(\d+)\.(\d+)(?:[/?#]|$)", re.IGNORECASE)
_browser_lock = asyncio.Lock()


class ShopeeAffiliateError(RuntimeError):
    pass


class ShopeeSessionMissing(ShopeeAffiliateError):
    pass


class ShopeeSessionExpired(ShopeeAffiliateError):
    pass


class ShopeeVerificationRequired(ShopeeAffiliateError):
    pass


class ShopeeUiChanged(ShopeeAffiliateError):
    pass


@dataclass(frozen=True)
class ResolvedShopeeUrl:
    source_url: str
    destination_url: str
    canonical_key: str


@dataclass(frozen=True)
class AffiliateConversion:
    source_url: str
    affiliate_url: str
    canonical_key: str
    from_cache: bool


def _is_shopee_host(hostname: str | None) -> bool:
    host = (hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in _SHOPEE_HOSTS)


def is_official_short_affiliate_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and (parts.hostname or "").lower() == "s.shopee.vn"


def _require_shopee_url(raw_url: str) -> str:
    try:
        url = normalize_public_http_url(raw_url)
    except WebReaderError as exc:
        raise ShopeeAffiliateError(str(exc)) from exc
    if not _is_shopee_host(urlsplit(url).hostname):
        raise ShopeeAffiliateError("Chỉ hỗ trợ link thuộc Shopee Việt Nam.")
    return url


def canonical_key_from_url(url: str) -> str:
    """Stable cache key without ever merging two distinct product destinations.

    Product IDs are the strongest key. For other Shopee pages, keep functional
    query parameters and remove only well-known tracking noise; dropping the whole
    query would be unsafe for universal-link URLs where the target may live there.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    match = _PRODUCT_PATH_RE.search(path) or _PRODUCT_SLUG_RE.search(path)
    if match:
        return f"item:{match.group(1)}:{match.group(2)}"
    query = parse_qs(parts.query)
    shop_id = (query.get("shopid") or query.get("shop_id") or [""])[0]
    item_id = (query.get("itemid") or query.get("item_id") or [""])[0]
    if str(shop_id).isdigit() and str(item_id).isdigit():
        return f"item:{shop_id}:{item_id}"

    tracking_keys = {
        "sp_atk", "xptdk", "smtt", "share_channel", "source", "from",
        "uls_trackid", "utm_source", "utm_medium", "utm_campaign",
        "utm_content", "utm_term",
    }
    functional = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in tracking_keys and not key.casefold().startswith("utm_")
    ]
    functional.sort()
    return urlunsplit((
        parts.scheme.lower(),
        (parts.hostname or "").lower(),
        path,
        urlencode(functional, doseq=True),
        "",
    ))


async def resolve_shopee_url(raw_url: str) -> ResolvedShopeeUrl:
    """Resolve Shopee short links without allowing redirects off Shopee/public IPs."""
    source = _require_shopee_url(raw_url)
    current = source
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
        )
    }
    timeout = httpx.Timeout(config.SHOPEE_RESOLVE_TIMEOUT_SEC)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code not in _REDIRECT_CODES:
                        destination = str(response.url)
                        return ResolvedShopeeUrl(
                            source_url=source,
                            destination_url=destination,
                            canonical_key=canonical_key_from_url(destination),
                        )
                    location = response.headers.get("location")
            except httpx.HTTPError as exc:
                raise ShopeeAffiliateError(f"Không mở được link Shopee: {exc}") from exc
            if not location:
                break
            candidate = urljoin(current, location)
            # Validate public DNS/IP on every hop and additionally keep the redirect
            # inside Shopee-owned hostnames. This preserves the SSRF fix.
            current = _require_shopee_url(candidate)
    raise ShopeeAffiliateError("Link Shopee chuyển hướng quá nhiều lần hoặc không hợp lệ.")


async def _block_heavy_resources(route) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


async def _page_scopes(page):
    """Return the main page plus child frames that can host the Custom Link UI.

    Shopee is a SPA and has changed how some dashboard sections are mounted over
    time.  Looking through frames as well as the top-level document costs almost
    nothing and avoids coupling the automation to one rendering strategy.
    """
    scopes = [page]
    try:
        for frame in page.frames:
            if frame is not page.main_frame:
                scopes.append(frame)
    except Exception:
        pass
    return scopes


async def _visible_input(page):
    """Find the Custom Link source field without assuming one Shopee DOM version."""
    selectors = [
        # Prefer explicit accessibility/placeholder hints first.
        'textarea[placeholder*="link" i]',
        'input[placeholder*="link" i]',
        'textarea[placeholder*="liên kết" i]',
        'input[placeholder*="liên kết" i]',
        'textarea[aria-label*="link" i]',
        'input[aria-label*="link" i]',
        'textarea[aria-label*="liên kết" i]',
        'input[aria-label*="liên kết" i]',
        # Some component libraries use an editable div rather than a native input.
        '[contenteditable="true"][role="textbox"]',
        '[contenteditable="true"]',
        # Last-resort native controls. Descriptor filtering below avoids obvious
        # Sub-ID/code/search fields.
        'textarea',
        'input[type="url"]',
        'input[type="text"]',
        'input:not([type])',
    ]
    for scope in await _page_scopes(page):
        for selector in selectors:
            try:
                locator = scope.locator(selector)
                count = await locator.count()
            except Exception:
                continue
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if not await item.is_visible():
                        continue
                    descriptor = " ".join(
                        filter(
                            None,
                            [
                                await item.get_attribute("placeholder"),
                                await item.get_attribute("aria-label"),
                                await item.get_attribute("name"),
                                await item.get_attribute("id"),
                            ],
                        )
                    ).casefold()
                    # Shopee exposes optional Sub ID fields near Custom Link. Do not
                    # accidentally put the product URL there. Search/header inputs are
                    # also poor fallbacks when the actual component is still loading.
                    rejected = (
                        "sub", "mã", "code", "search", "tìm kiếm", "keyword",
                        "campaign", "chiến dịch",
                    )
                    if any(token in descriptor for token in rejected):
                        continue
                    return item
                except Exception:
                    continue
    return None


async def _wait_for_custom_link_field(page):
    """Wait for the SPA to render the Custom Link field.

    ``domcontentloaded`` only means the JS shell was downloaded. On Render Free
    Shopee's React/Vue bundle can take several more seconds to mount the actual
    dashboard component, so probing once caused false "UI changed" failures.
    """
    timeout_sec = max(5, config.SHOPEE_BROWSER_ACTION_TIMEOUT_SEC * 2)
    deadline = asyncio.get_running_loop().time() + timeout_sec
    last_url = page.url
    while asyncio.get_running_loop().time() < deadline:
        await _assert_logged_in(page)
        field = await _visible_input(page)
        if field is not None:
            return field
        last_url = page.url
        await asyncio.sleep(0.5)

    # Keep the user-facing error concise; detailed DOM information is written to
    # logs without cookies/storage state so debugging production remains safe.
    try:
        frame_urls = [frame.url for frame in page.frames][:6]
        button_texts: list[str] = []
        for scope in await _page_scopes(page):
            try:
                texts = await scope.locator('button, [role="button"]').all_inner_texts()
                button_texts.extend(t.strip() for t in texts if t.strip())
            except Exception:
                continue
        logger.warning(
            "Shopee Custom Link field not found after %.1fs; url=%s frames=%r buttons=%r",
            timeout_sec, last_url, frame_urls, button_texts[:15],
        )
    except Exception:
        logger.warning("Shopee Custom Link field not found; diagnostic collection failed", exc_info=True)
    raise ShopeeUiChanged(
        "Không tìm thấy ô nhập Custom Link sau khi chờ giao diện Shopee tải xong. "
        "Session có thể đang ở sai loại tài khoản/trang hoặc Shopee vừa đổi giao diện."
    )


async def _click_get_link(page) -> None:
    patterns = (
        r"^\s*Lấy\s*link\s*$",
        r"^\s*Lấy\s*liên\s*kết\s*$",
        r"^\s*Tạo\s*link\s*$",
        r"^\s*Tạo\s*liên\s*kết\s*$",
        r"^\s*Get\s*link\s*$",
        r"^\s*Generate\s*link\s*$",
        r"^\s*Convert\s*$",
    )
    for scope in await _page_scopes(page):
        for pattern in patterns:
            try:
                candidate = scope.get_by_role("button", name=re.compile(pattern, re.IGNORECASE))
                count = await candidate.count()
            except Exception:
                continue
            for idx in range(count):
                button = candidate.nth(idx)
                try:
                    if await button.is_visible() and await button.is_enabled():
                        await button.click()
                        return
                except Exception:
                    continue

    # Some Shopee UI versions render a div/span styled as a button.
    for scope in await _page_scopes(page):
        for label in (
            "Lấy link", "Lấy liên kết", "Tạo link", "Tạo liên kết",
            "Get link", "Generate link", "Convert",
        ):
            try:
                candidate = scope.get_by_text(label, exact=True)
                count = await candidate.count()
            except Exception:
                continue
            for idx in range(count):
                node = candidate.nth(idx)
                try:
                    if await node.is_visible():
                        await node.click()
                        return
                except Exception:
                    continue
    raise ShopeeUiChanged("Không tìm thấy nút “Lấy link” trên Shopee Affiliate.")


async def _extract_affiliate_url(page, source_url: str) -> str | None:
    candidates: list[str] = []
    for scope in await _page_scopes(page):
        try:
            values = await scope.locator("input, textarea").evaluate_all(
                "els => els.map(e => e.value || '').filter(Boolean)"
            )
            candidates.extend(str(value) for value in values)
        except Exception:
            pass
        try:
            hrefs = await scope.locator("a[href]").evaluate_all(
                "els => els.map(e => e.href || '').filter(Boolean)"
            )
            candidates.extend(str(value) for value in hrefs)
        except Exception:
            pass
        try:
            body_text = await scope.locator("body").inner_text(timeout=2_000)
            candidates.append(body_text)
        except Exception:
            pass
    for candidate in candidates:
        for match in _AFFILIATE_URL_RE.findall(str(candidate)):
            if match != source_url:
                return match.rstrip(".,);]}")
    return None


async def _assert_logged_in(page) -> None:
    url = page.url.casefold()
    if "login" in url or "signin" in url:
        raise ShopeeSessionExpired("Phiên Shopee Affiliate đã hết hạn; hãy nạp lại session trong /admin.")
    try:
        text = (await page.locator("body").inner_text(timeout=3_000)).casefold()
    except Exception:
        return
    challenge_markers = (
        "captcha",
        "xác minh bảo mật",
        "security verification",
        "verify you are human",
        "xác nhận bạn không phải robot",
    )
    if any(marker in text for marker in challenge_markers):
        raise ShopeeVerificationRequired(
            "Shopee đang yêu cầu CAPTCHA/xác minh thủ công; bot không tự vượt bước này."
        )
    # Login pages occasionally keep the custom-link URL while rendering auth in-place.
    login_markers = ("đăng nhập bằng sms", "đăng nhập vào shopee", "login with sms")
    if any(marker in text for marker in login_markers):
        raise ShopeeSessionExpired("Phiên Shopee Affiliate đã hết hạn; hãy nạp lại session trong /admin.")


async def _convert_on_page(page, destination_url: str) -> str:
    await page.goto(
        config.SHOPEE_AFFILIATE_CUSTOM_LINK_URL,
        wait_until="domcontentloaded",
        timeout=config.SHOPEE_BROWSER_NAV_TIMEOUT_SEC * 1000,
    )
    await _assert_logged_in(page)
    # Some authenticated sessions first land on /dashboard before the router has
    # restored the requested SPA route. Retry the official Custom Link URL once.
    if "/offer/custom_link" not in urlsplit(page.url).path.casefold():
        await asyncio.sleep(1)
        await page.goto(
            config.SHOPEE_AFFILIATE_CUSTOM_LINK_URL,
            wait_until="domcontentloaded",
            timeout=config.SHOPEE_BROWSER_NAV_TIMEOUT_SEC * 1000,
        )
        await _assert_logged_in(page)
    field = await _wait_for_custom_link_field(page)
    await field.fill(destination_url)
    await _click_get_link(page)

    deadline = asyncio.get_running_loop().time() + config.SHOPEE_BROWSER_RESULT_TIMEOUT_SEC
    while asyncio.get_running_loop().time() < deadline:
        await _assert_logged_in(page)
        affiliate_url = await _extract_affiliate_url(page, destination_url)
        if affiliate_url:
            if (urlsplit(affiliate_url).hostname or "").lower() != "s.shopee.vn":
                raise ShopeeUiChanged("Shopee trả link không phải dạng s.shopee.vn như yêu cầu.")
            return affiliate_url
        await asyncio.sleep(0.5)
    raise ShopeeUiChanged("Shopee không trả về link affiliate rút gọn trong thời gian chờ.")


async def _launch_and_convert(resolved: list[ResolvedShopeeUrl]) -> dict[str, str]:
    state = await shopee_affiliate_session.load()
    if not state:
        raise ShopeeSessionMissing(
            "Chưa có session Shopee Affiliate. Vào /admin → Shopee Affiliate để nạp session trước."
        )

    # Lazy import keeps the rest of the bot/test suite usable even on machines that
    # intentionally do not install the optional browser dependency.
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ShopeeAffiliateError("Thiếu dependency Playwright trên runtime.") from exc

    results: dict[str, str] = {}
    browser = None
    context = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--mute-audio",
                    "--no-first-run",
                    "--disable-default-apps",
                    "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints",
                ],
            )
            context = await browser.new_context(
                storage_state=state,
                viewport={"width": 1024, "height": 768},
                locale="vi-VN",
            )
            context.set_default_timeout(config.SHOPEE_BROWSER_ACTION_TIMEOUT_SEC * 1000)
            page = await context.new_page()
            await page.route("**/*", _block_heavy_resources)

            for item in resolved:
                affiliate_url = await _convert_on_page(page, item.destination_url)
                results[item.canonical_key] = affiliate_url

            # Shopee may rotate auth cookies during normal navigation. Persist the
            # refreshed state before closing the ephemeral browser.
            await shopee_affiliate_session.save(
                await context.storage_state(indexed_db=True, opfs=True)
            )
    except PlaywrightTimeoutError as exc:
        raise ShopeeAffiliateError("Shopee Affiliate phản hồi quá chậm hoặc giao diện chưa tải xong.") from exc
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                logger.debug("Không đóng được Shopee browser context", exc_info=True)
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.debug("Không đóng được Shopee browser", exc_info=True)
    return results


async def convert_urls(account_id: str, source_urls: list[str]) -> list[AffiliateConversion]:
    """Convert all URLs with at most one Chromium launch for the whole batch."""
    unique_sources = list(dict.fromkeys(source_urls))
    if not unique_sources:
        return []

    # Direct-source cache is the cheapest possible path: no network and no browser.
    direct_cached = {
        source: affiliate
        for source, affiliate in (
            await facebook_repository.get_affiliate_links(account_id, unique_sources)
        ).items()
        if is_official_short_affiliate_url(affiliate)
    }
    results: dict[str, AffiliateConversion] = {
        source: AffiliateConversion(source, affiliate, "", True)
        for source, affiliate in direct_cached.items()
    }
    unresolved = [source for source in unique_sources if source not in direct_cached]
    if not unresolved:
        return [results[source] for source in unique_sources]

    resolved_items = await asyncio.gather(*(resolve_shopee_url(url) for url in unresolved))
    canonical_cached = {
        key: affiliate
        for key, affiliate in (
            await facebook_repository.get_affiliate_links_by_canonical(
                account_id,
                [item.canonical_key for item in resolved_items],
            )
        ).items()
        if is_official_short_affiliate_url(affiliate)
    }
    missing_by_key: dict[str, ResolvedShopeeUrl] = {}
    for item in resolved_items:
        cached_url = canonical_cached.get(item.canonical_key)
        if cached_url:
            await facebook_repository.set_affiliate_link(
                account_id,
                item.source_url,
                cached_url,
                canonical_key=item.canonical_key,
            )
            results[item.source_url] = AffiliateConversion(
                item.source_url, cached_url, item.canonical_key, True
            )
        else:
            missing_by_key.setdefault(item.canonical_key, item)

    if missing_by_key:
        async with _browser_lock:
            # Another queued request may have populated the cache while this one waited.
            second_cache = {
                key: affiliate
                for key, affiliate in (
                    await facebook_repository.get_affiliate_links_by_canonical(
                        account_id, list(missing_by_key)
                    )
                ).items()
                if is_official_short_affiliate_url(affiliate)
            }
            to_convert = [item for key, item in missing_by_key.items() if key not in second_cache]
            if to_convert:
                try:
                    async with asyncio.timeout(config.SHOPEE_BROWSER_TOTAL_TIMEOUT_SEC):
                        generated = await _launch_and_convert(to_convert)
                except TimeoutError as exc:
                    raise ShopeeAffiliateError(
                        "Shopee Affiliate vượt quá thời gian xử lý; Chromium đã được hủy để tránh treo bot. "
                        "Thử lại một lần, hoặc nạp lại session nếu lỗi lặp lại."
                    ) from exc
            else:
                generated = {}
            generated.update(second_cache)

            for item in resolved_items:
                if item.source_url in results:
                    continue
                affiliate_url = generated.get(item.canonical_key)
                if not affiliate_url:
                    raise ShopeeAffiliateError("Không tạo được affiliate link cho một link Shopee.")
                await facebook_repository.set_affiliate_link(
                    account_id,
                    item.source_url,
                    affiliate_url,
                    canonical_key=item.canonical_key,
                )
                results[item.source_url] = AffiliateConversion(
                    item.source_url,
                    affiliate_url,
                    item.canonical_key,
                    item.canonical_key in second_cache,
                )

    return [results[source] for source in unique_sources]


async def convert_url(account_id: str, source_url: str) -> AffiliateConversion:
    return (await convert_urls(account_id, [source_url]))[0]
