#!/usr/bin/env python3
"""Export and verify an authenticated Shopee Affiliate browser session.

Run locally (not on Render), log in manually including OTP/CAPTCHA, then upload
``shopee-storage-state.json`` in /admin -> Shopee Affiliate.  Playwright 1.63 can
persist cookies/localStorage plus IndexedDB and OPFS, which makes SPA login state
far more portable than the older cookie/localStorage-only export.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

CUSTOM_LINK_URL = "https://affiliate.shopee.vn/offer/custom_link"


def _find_custom_link_field(page, timeout_sec: int = 25) -> bool:
    selectors = (
        'textarea[placeholder*="link" i]',
        'input[placeholder*="link" i]',
        'textarea[placeholder*="liên kết" i]',
        'input[placeholder*="liên kết" i]',
        '[contenteditable="true"][role="textbox"]',
        'textarea',
    )
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        for frame in page.frames:
            for selector in selectors:
                try:
                    locator = frame.locator(selector)
                    for idx in range(locator.count()):
                        if locator.nth(idx).is_visible():
                            return True
                except Exception:
                    continue
        time.sleep(0.5)
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="shopee-storage-state.json")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Thiếu Playwright. Chạy: pip install playwright==1.63.0 && playwright install chromium"
        ) from exc

    output = Path(args.output).expanduser().resolve()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(locale="vi-VN")
        page = context.new_page()
        page.goto(CUSTOM_LINK_URL, wait_until="domcontentloaded", timeout=90_000)

        print("\nĐăng nhập Shopee Affiliate trong cửa sổ vừa mở.")
        print("Hoàn tất OTP/CAPTCHA nếu Shopee yêu cầu.")
        print("Sau khi đăng nhập xong, không cần tự đóng trình duyệt.")
        input("Quay lại terminal và nhấn ENTER để kiểm tra rồi xuất session... ")

        # Re-open the official route after authentication. Login flows often return
        # to /dashboard rather than the originally requested Custom Link route.
        page.goto(CUSTOM_LINK_URL, wait_until="domcontentloaded", timeout=90_000)
        if "login" in page.url.casefold() or "signin" in page.url.casefold():
            browser.close()
            raise SystemExit(
                "Phiên đăng nhập chưa hợp lệ: Shopee vẫn chuyển về trang login. "
                "Hãy chạy script lại và hoàn tất đăng nhập/OTP trước khi nhấn ENTER."
            )

        if not _find_custom_link_field(page):
            print("\nCẢNH BÁO: đã đăng nhập nhưng chưa thấy ô Custom Link trong 25 giây.")
            print(f"Trang hiện tại: {page.url}")
            print("Bạn có thể kiểm tra cửa sổ trình duyệt; nếu Shopee đang yêu cầu xác minh, hãy hoàn tất rồi chạy lại script.")
        else:
            print("\nOK: đã thấy giao diện Custom Link trên phiên đăng nhập hiện tại.")

        # Include IndexedDB + OPFS (supported by Playwright 1.63) so the state used
        # on Render is as complete as possible for modern SPA authentication.
        state = context.storage_state(indexed_db=True, opfs=True)
        output.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        # Verify portability: restore into a fresh context, exactly like Render does.
        verify = browser.new_context(storage_state=state, locale="vi-VN")
        verify_page = verify.new_page()
        verify_page.goto(CUSTOM_LINK_URL, wait_until="domcontentloaded", timeout=90_000)
        restored_ok = (
            "login" not in verify_page.url.casefold()
            and "signin" not in verify_page.url.casefold()
            and _find_custom_link_field(verify_page, timeout_sec=25)
        )
        verify.close()
        browser.close()

    print(f"\nĐã lưu session tại: {output}")
    if restored_ok:
        print("VERIFY OK: session mở lại được Custom Link trong browser context mới.")
    else:
        print("VERIFY WARNING: session đã lưu nhưng context mới chưa thấy Custom Link.")
        print("Không nên upload state này lên Render cho đến khi VERIFY OK.")
    print("Mở file này, copy toàn bộ JSON và dán vào /admin -> Shopee Affiliate.")
    print("Xóa file sau khi nạp xong vì nó chứa cookie/token đăng nhập nhạy cảm.")


if __name__ == "__main__":
    main()
