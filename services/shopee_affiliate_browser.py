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


_INPUT_SELECTORS_JS = [
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
_REJECTED_DESCRIPTOR_TOKENS = [
    # Shopee exposes optional Sub ID fields near Custom Link. Do not
    # accidentally put the product URL there. Search/header inputs are also
    # poor fallbacks when the actual component is still loading.
    "sub", "mã", "code", "search", "tìm kiếm", "keyword", "campaign", "chiến dịch",
]
# Runs entirely inside the browser in ONE round trip. The previous version did
# one Playwright<->Chromium round trip per selector/attribute (locator.count(),
# then is_visible()/get_attribute() x4 per match) - on a CPU-starved host each
# of those round trips can itself take seconds, so a single poll of
# _wait_for_custom_link_field could easily need 100+ round trips and blow
# through the whole budget without ever actually being stuck on anything.
_FIND_INPUT_JS = """
([selectors, rejected]) => {
    for (const sel of selectors) {
        let els;
        try { els = document.querySelectorAll(sel); } catch (e) { continue; }
        for (const el of els) {
            const rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) continue;
            const style = window.getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none') continue;
            const descriptor = [
                el.getAttribute('placeholder'), el.getAttribute('aria-label'),
                el.getAttribute('name'), el.getAttribute('id'),
            ].filter(Boolean).join(' ').toLowerCase();
            if (rejected.some((t) => descriptor.includes(t))) continue;
            return el;
        }
    }
    return null;
}
"""


async def _visible_input(page):
    """Find the Custom Link source field without assuming one Shopee DOM version."""
    for scope in await _page_scopes(page):
        try:
            handle = await scope.evaluate_handle(
                _FIND_INPUT_JS, [_INPUT_SELECTORS_JS, _REJECTED_DESCRIPTOR_TOKENS]
            )
            element = handle.as_element()
        except Exception:
            continue
        if element is not None:
            return element
    return None


_PAGE_SNAPSHOT_JS = """
() => {
    const body = document.body;
    return {
        readyState: document.readyState,
        title: document.title,
        elementCount: document.querySelectorAll('*').length,
        scriptCount: document.querySelectorAll('script').length,
        bodyTextSnippet: (body && body.innerText ? body.innerText : '').slice(0, 300),
        hasSpinnerLike: !!document.querySelector(
            '[class*="loading" i], [class*="spinner" i], [class*="skeleton" i]'
        ),
    };
}
"""


async def _wait_for_custom_link_field(page):
    """Wait for the SPA to render the Custom Link field.

    ``domcontentloaded`` only means the JS shell was downloaded. On Render Free
    Shopee's React/Vue bundle can take several more seconds to mount the actual
    dashboard component, so probing once caused false "UI changed" failures.
    """
    timeout_sec = config.SHOPEE_BROWSER_FIELD_WAIT_SEC
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
        try:
            snapshot = await page.evaluate(_PAGE_SNAPSHOT_JS)
        except Exception:
            snapshot = None
        logger.warning(
            "Shopee Custom Link field not found after %.1fs; url=%s frames=%r buttons=%r snapshot=%r",
            timeout_sec, last_url, frame_urls, button_texts[:15], snapshot,
        )
    except Exception:
        logger.warning("Shopee Custom Link field not found; diagnostic collection failed", exc_info=True)
    raise ShopeeUiChanged(
        "Không tìm thấy ô nhập Custom Link sau khi chờ giao diện Shopee tải xong. "
        "Session có thể đang ở sai loại tài khoản/trang hoặc Shopee vừa đổi giao diện."
    )


_BUTTON_TEXTS_JS = [
    "lấy link", "lấy liên kết", "tạo link", "tạo liên kết",
    "get link", "generate link", "convert",
]
# Same idea as _FIND_INPUT_JS: one round trip instead of up to
# scopes x patterns role-lookups (get_by_role does accessibility-tree work,
# which is even slower per round trip than a plain DOM query) plus a second
# full scopes x labels fallback pass for div/span-styled buttons. This single
# pass covers both real buttons and styled divs/spans/links at once.
_FIND_BUTTON_JS = """
([texts]) => {
    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        const style = window.getComputedStyle(el);
        return style.visibility !== 'hidden' && style.display !== 'none';
    };
    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const nodes = document.querySelectorAll('button, [role="button"], div, span, a');
    for (const el of nodes) {
        const text = norm(el.textContent);
        if (!text || text.length > 40 || !texts.includes(text)) continue;
        if (!isVisible(el)) continue;
        if (el.disabled) continue;
        if (el.getAttribute('aria-disabled') === 'true') continue;
        return el;
    }
    return null;
}
"""


async def _click_get_link(page) -> None:
    for scope in await _page_scopes(page):
        try:
            handle = await scope.evaluate_handle(_FIND_BUTTON_JS, [_BUTTON_TEXTS_JS])
            element = handle.as_element()
        except Exception:
            continue
        if element is not None:
            await element.click()
            return
    raise ShopeeUiChanged("Không tìm thấy nút “Lấy link” trên Shopee Affiliate.")


_EXTRACT_CANDIDATES_JS = """
() => {
    const out = [];
    document.querySelectorAll('input, textarea').forEach((el) => {
        if (el.value) out.push(el.value);
    });
    document.querySelectorAll('a[href]').forEach((el) => {
        if (el.href) out.push(el.href);
    });
    if (document.body && document.body.innerText) out.push(document.body.innerText);
    return out;
}
"""


async def _extract_affiliate_url(page, source_url: str) -> str | None:
    candidates: list[str] = []
    for scope in await _page_scopes(page):
        try:
            values = await scope.evaluate(_EXTRACT_CANDIDATES_JS)
        except Exception:
            continue
        if values:
            candidates.extend(str(value) for value in values)
    for candidate in candidates:
        for match in _AFFILIATE_URL_RE.findall(candidate):
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
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    logger.info("Shopee convert: bắt đầu goto Custom Link cho %s", destination_url)
    await page.goto(
        config.SHOPEE_AFFILIATE_CUSTOM_LINK_URL,
        wait_until="domcontentloaded",
        timeout=config.SHOPEE_BROWSER_NAV_TIMEOUT_SEC * 1000,
    )
    logger.info("Shopee convert: goto #1 xong sau %.1fs, url hiện tại=%s", loop.time() - t0, page.url)
    _log_memory("sau goto #1")
    await _assert_logged_in(page)
    # Some authenticated sessions first land on /dashboard before the router has
    # restored the requested SPA route. Retry the official Custom Link URL once.
    if "/offer/custom_link" not in urlsplit(page.url).path.casefold():
        logger.info(
            "Shopee convert: route chưa đúng (%s), thử goto lại lần 2", page.url
        )
        await asyncio.sleep(1)
        t1 = loop.time()
        await page.goto(
            config.SHOPEE_AFFILIATE_CUSTOM_LINK_URL,
            wait_until="domcontentloaded",
            timeout=config.SHOPEE_BROWSER_NAV_TIMEOUT_SEC * 1000,
        )
        logger.info("Shopee convert: goto #2 xong sau %.1fs, url hiện tại=%s", loop.time() - t1, page.url)
        await _assert_logged_in(page)
    field = await _wait_for_custom_link_field(page)
    logger.info("Shopee convert: đã thấy ô Custom Link sau %.1fs tổng cộng", loop.time() - t0)
    await field.fill(destination_url)
    await _click_get_link(page)
    logger.info("Shopee convert: đã bấm Lấy link, chờ kết quả (tổng %.1fs)", loop.time() - t0)

    deadline = loop.time() + config.SHOPEE_BROWSER_RESULT_TIMEOUT_SEC
    while loop.time() < deadline:
        await _assert_logged_in(page)
        affiliate_url = await _extract_affiliate_url(page, destination_url)
        if affiliate_url:
            if (urlsplit(affiliate_url).hostname or "").lower() != "s.shopee.vn":
                raise ShopeeUiChanged("Shopee trả link không phải dạng s.shopee.vn như yêu cầu.")
            logger.info(
                "Shopee convert: có link affiliate sau %.1fs tổng cộng", loop.time() - t0
            )
            return affiliate_url
        await asyncio.sleep(0.5)
    logger.warning(
        "Shopee convert: hết %.1fs chờ kết quả mà không thấy link affiliate; url hiện tại=%s",
        config.SHOPEE_BROWSER_RESULT_TIMEOUT_SEC, page.url,
    )
    raise ShopeeUiChanged("Shopee không trả về link affiliate rút gọn trong thời gian chờ.")


def _read_int(path: str) -> int | None:
    try:
        with open(path) as fh:
            raw = fh.read().strip()
        return None if raw in {"", "max"} else int(raw)
    except Exception:
        return None


def _log_memory(tag: str) -> None:
    """Log container memory usage vs its REAL limit (cgroup), not /proc/meminfo.

    Inside a container /proc/meminfo reports the physical HOST's RAM (e.g. 16GB
    on a Render node), not the plan's limit (512MB on Free), so it can never
    show an OOM coming. The limit is enforced by the cgroup.
    """
    usage = _read_int("/sys/fs/cgroup/memory.current")  # cgroup v2
    limit = _read_int("/sys/fs/cgroup/memory.max")
    if usage is None:  # cgroup v1
        usage = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        limit = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if usage is None:
        return
    if limit is not None and limit > 1 << 50:  # v1 reports a huge number for "no limit"
        limit = None
    logger.info(
        "Shopee convert: [mem] container dùng %.0fMB / giới hạn %s (%s)",
        usage / 1048576,
        f"{limit / 1048576:.0f}MB" if limit else "không rõ",
        tag,
    )


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
    loop = asyncio.get_running_loop()
    t_start = loop.time()
    engine_name = config.SHOPEE_BROWSER_ENGINE
    _log_memory(f"trước khi khởi động {engine_name}")
    try:
        async with async_playwright() as playwright:
            if config.SHOPEE_BROWSER_CDP_URL:
                logger.info(
                    "Shopee convert: đang kết nối browser bên ngoài qua CDP (%s) cho %d link...",
                    config.SHOPEE_BROWSER_CDP_URL, len(resolved),
                )
                browser = await playwright.chromium.connect_over_cdp(config.SHOPEE_BROWSER_CDP_URL)
            else:
                logger.info("Shopee convert: đang khởi động %s cho %d link...", engine_name, len(resolved))
                if engine_name == "webkit":
                    # WebKit does not understand Chromium's command-line switches
                    # (--no-sandbox, --disable-gpu, --js-flags, ...); passing any of
                    # them would just fail to launch. It needs no extra args here.
                    engine = playwright.webkit
                    launch_kwargs: dict = {}
                else:
                    engine = playwright.chromium
                    launch_kwargs = {
                        "args": [
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
                            # Memory, not just CPU/time, looks like the real ceiling on
                            # a small Render instance (see _log_memory docstring above).
                            "--disable-background-timer-throttling",
                            "--disable-renderer-backgrounding",
                            "--blink-settings=imagesEnabled=false",
                            "--js-flags=--max-old-space-size=192",
                            "--renderer-process-limit=1",
                            "--disable-software-rasterizer",
                        ],
                    }
                browser = await engine.launch(headless=True, **launch_kwargs)
            logger.info("Shopee convert: %s đã sẵn sàng sau %.1fs", engine_name, loop.time() - t_start)
            _log_memory(f"sau khi {engine_name} sẵn sàng")
            context = await browser.new_context(
                storage_state=state,
                viewport={"width": 1024, "height": 768},
                locale="vi-VN",
            )
            context.set_default_timeout(config.SHOPEE_BROWSER_ACTION_TIMEOUT_SEC * 1000)
            page = await context.new_page()
            await page.route("**/*", _block_heavy_resources)

            for idx, item in enumerate(resolved, 1):
                t_item = loop.time()
                logger.info(
                    "Shopee convert: [%d/%d] bắt đầu %s", idx, len(resolved), item.destination_url
                )
                affiliate_url = await _convert_on_page(page, item.destination_url)
                results[item.canonical_key] = affiliate_url
                logger.info(
                    "Shopee convert: [%d/%d] xong sau %.1fs (tổng batch %.1fs)",
                    idx, len(resolved), loop.time() - t_item, loop.time() - t_start,
                )

            # Shopee may rotate auth cookies during normal navigation. Persist the
            # refreshed state before closing the ephemeral browser.
            await shopee_affiliate_session.save(
                await context.storage_state(indexed_db=True, opfs=True)
            )
    except PlaywrightTimeoutError as exc:
        raise ShopeeAffiliateError("Shopee Affiliate phản hồi quá chậm hoặc giao diện chưa tải xong.") from exc
    except ShopeeAffiliateError:
        raise
    except Exception as exc:
        # Anything else (engine-specific Playwright error, browser crash, ...)
        # used to escape as a raw 500 from the Zalo bridge with no message for
        # the user. Log the real traceback and surface a readable error.
        logger.exception("Shopee convert: lỗi không mong đợi trong lúc chạy %s", engine_name)
        raise ShopeeAffiliateError(
            f"Lỗi khi chạy browser ({engine_name}): {type(exc).__name__}: {str(exc)[:200]}"
        ) from exc
    finally:
        logger.info(
            "Shopee convert: đóng browser sau %.1fs tổng cộng cho batch %d link (%d link xong)",
            loop.time() - t_start, len(resolved), len(results),
        )
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


def _browser_batch_timeout_sec(item_count: int) -> float:
    """Worst-case time a healthy ``_launch_and_convert`` run can legitimately need.

    ``SHOPEE_BROWSER_TOTAL_TIMEOUT_SEC`` used to be a flat 90s regardless of the
    other Shopee timeouts. With the default sub-timeouts (nav 35s tried up to
    twice for the SPA dashboard/custom-link redirect, field-wait
    ``SHOPEE_BROWSER_FIELD_WAIT_SEC``, result poll 20s) a single item alone can
    legitimately need well over 120s, before even counting Chromium's own cold
    start on a small Render instance. That made
    the outer safety timeout fire on slow-but-otherwise-working runs, which
    then told the person to reload their session for a problem that was
    really just not enough time budgeted. This computes a floor from the
    actual configured sub-timeouts (times the number of items sharing this
    one browser session) so the outer timeout only fires on a genuine hang.
    ``SHOPEE_BROWSER_TOTAL_TIMEOUT_SEC`` still applies as a minimum/override
    for anyone who wants a larger explicit ceiling.
    """
    per_item = (
        config.SHOPEE_BROWSER_NAV_TIMEOUT_SEC * 2  # first nav + one SPA-redirect retry
        + 1  # sleep between the two navigations
        + config.SHOPEE_BROWSER_FIELD_WAIT_SEC  # SPA mount wait
        + config.SHOPEE_BROWSER_ACTION_TIMEOUT_SEC  # fill/click
        + config.SHOPEE_BROWSER_RESULT_TIMEOUT_SEC  # result poll
    )
    launch_budget = config.SHOPEE_BROWSER_LAUNCH_BUDGET_SEC  # Chromium cold start on a small Render instance
    return max(
        config.SHOPEE_BROWSER_TOTAL_TIMEOUT_SEC,
        launch_budget + per_item * max(1, item_count),
    )


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
        logger.info(
            "Shopee convert: %d link cần browser (%s); đang chờ tới lượt (_browser_lock)...",
            len(missing_by_key), ", ".join(list(missing_by_key)[:5]),
        )
        lock_wait_t0 = asyncio.get_running_loop().time()
        async with _browser_lock:
            lock_wait_sec = asyncio.get_running_loop().time() - lock_wait_t0
            if lock_wait_sec > 1:
                logger.info("Shopee convert: chờ _browser_lock mất %.1fs", lock_wait_sec)
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
                budget = _browser_batch_timeout_sec(len(to_convert))
                logger.info(
                    "Shopee convert: bắt đầu batch %d link, ngân sách timeout %.1fs",
                    len(to_convert), budget,
                )
                try:
                    async with asyncio.timeout(budget):
                        generated = await _launch_and_convert(to_convert)
                except TimeoutError as exc:
                    logger.warning(
                        "Shopee convert: hết ngân sách %.1fs cho %d link — xem log 'Shopee convert:' phía "
                        "trên để biết dừng ở bước nào (goto/field/click/result).",
                        budget, len(to_convert),
                    )
                    raise ShopeeAffiliateError(
                        "Shopee Affiliate vượt quá thời gian xử lý; trình duyệt đã được hủy để tránh treo bot. "
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
