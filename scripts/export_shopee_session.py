#!/usr/bin/env python3
"""One-time local helper to export an authenticated Shopee Affiliate session.

Run this on your own computer (not Render), log in manually including OTP/CAPTCHA,
then paste the generated JSON into /admin -> Shopee Affiliate. The production bot
never stores your Shopee password; only Playwright storage_state is encrypted in DB.
"""
from __future__ import annotations

import argparse
from pathlib import Path


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
        page.goto(
            "https://affiliate.shopee.vn/offer/custom_link",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        print("\nĐăng nhập Shopee Affiliate trong cửa sổ vừa mở.")
        print("Hoàn tất OTP/CAPTCHA nếu Shopee yêu cầu và mở được trang Custom Link.")
        input("Sau đó quay lại terminal và nhấn ENTER để xuất session... ")
        context.storage_state(path=str(output))
        browser.close()

    print(f"Đã lưu session tại: {output}")
    print("Mở file này, copy toàn bộ JSON và dán vào /admin -> Shopee Affiliate.")
    print("Xóa file sau khi nạp xong vì nó chứa cookie đăng nhập nhạy cảm.")


if __name__ == "__main__":
    main()
