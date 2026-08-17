"""
diagnose_router9.py
-----------------------------------------

Script chẩn đoán 9Router (gateway OpenAI-compatible dùng làm provider đầu
tiên của provider-chain, thay cho cookie Gemini trước đây).

Chạy trên Render:

    https://your-app.onrender.com/diagnose  (header X-Diagnose-Token: <DIAGNOSE_SECRET>)

hoặc

    python diagnose_router9.py
"""

from __future__ import annotations

import asyncio
import traceback

from ai import orchestrator, router9_client
from core import config

LINE = "=" * 70


def title(text: str):
    print()
    print(LINE)
    print(text)
    print(LINE)


async def main():
    title("THÔNG TIN MÔI TRƯỜNG")

    print(f"ROUTER9_BASE_URL : {config.ROUTER9_BASE_URL}")
    print(f"ROUTER9_MODEL    : {config.ROUTER9_MODEL}")
    print(f"ROUTER9_API_KEY  : {'OK' if config.ROUTER9_API_KEY else 'MISSING'}")
    print()

    if not config.ROUTER9_API_KEY:
        print("Không có API key 9Router.")
        return

    title("LIST MODELS")

    try:
        models = await router9_client.list_models()
        if not models:
            print("Không lấy được danh sách model (gateway có thể không hỗ trợ /models).")
        else:
            print(f"Số model: {len(models)}")
            for model in models:
                print(" -", model)
    except Exception:
        traceback.print_exc()

    title("TEST TEXT")

    prompt = "Trả lời ngắn gọn 1 câu: bạn đang hoạt động bình thường chứ?"

    try:
        response = await orchestrator.ask(prompt)

        print("Response class:", type(response))
        print()

        text = getattr(response, "text", "")

        print("TEXT")
        print("--------------------------------")

        if text:
            print(text)
        else:
            print("(empty)")

    except Exception:
        traceback.print_exc()

    title("DONE")


if __name__ == "__main__":
    asyncio.run(main())
