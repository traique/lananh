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


async def _visible_input(page):
    selectors = [
        'textarea[placeholder*="link" i]',
        'input[placeholder*="link" i]',
        'textarea[aria-label*="link" i]',
        'input[aria-label*="link" i]',
        'textarea',
        'input[type="text"]',
        'input:not([type])',
    ]
    for selector in selectors:
        locator = page.locator(selector)
        for idx in range(await locator.count()):
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
                        ],
                    )
                ).casefold()
                if "sub" in descriptor or "mã" in descriptor or "code" in descriptor:
                    continue
                return item
            except Exception:
                continue
    return None


async def _click_get_link(page) -> None:
    patterns = (r"^\s*Lấy\s*link\s*$", r"^\s*Tạo\s*link\s*$", r"^\s*Get\s*link\s*$")
    for pattern in patterns:
        candidate = page.get_by_role("button", name=re.compile(pattern, re.IGNORECASE))
        if await candidate.count():
            for idx in range(await candidate.count()):
                button = candidate.nth(idx)
                if await button.is_visible() and await button.is_enabled():
                    await button.click()
                    return
    # Some Shopee UI versions render a div/span styled as a button.
    for text in ("Lấy link", "Tạo link", "Get link"):
        candidate = page.get_by_text(text, exact=True)
        for idx in range(await candidate.count()):
            node = candidate.nth(idx)
            if await node.is_visible():
                await node.click()
                return
    raise ShopeeUiChanged("Không tìm thấy nút “Lấy link” trên Shopee Affiliate.")


async def _extract_affiliate_url(page, source_url: str) -> str | None:
    values = await page.locator("input, textarea").evaluate_all(
        "els => els.map(e => e.value || '').filter(Boolean)"
    )
    hrefs = await page.locator("a[href]").evaluate_all(
        "els => els.map(e => e.href || '').filter(Boolean)"
    )
    candidates = [*values, *hrefs]
    try:
        body_text = await page.locator("body").inner_text(timeout=2_000)
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
    field = await _visible_input(page)
    if field is None:
        raise ShopeeUiChanged("Không tìm thấy ô nhập Custom Link trên Shopee Affiliate.")
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
                service_workers="block",
            )
            context.set_default_timeout(config.SHOPEE_BROWSER_ACTION_TIMEOUT_SEC * 1000)
            page = await context.new_page()
            await page.route("**/*", _block_heavy_resources)

            for item in resolved:
                affiliate_url = await _convert_on_page(page, item.destination_url)
                results[item.canonical_key] = affiliate_url

            # Shopee may rotate auth cookies during normal navigation. Persist the
            # refreshed state before closing the ephemeral browser.
            await shopee_affiliate_session.save(await context.storage_state())
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
            generated = await _launch_and_convert(to_convert) if to_convert else {}
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
