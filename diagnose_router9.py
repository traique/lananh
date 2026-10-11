"""
diagnose_router9.py
-----------------------------------------

Script chẩn đoán 9Router (gateway OpenAI-compatible dùng làm provider đầu
tiên của provider-chain, thay cho cookie Gemini trước đây).

Chạy trên Render:

    https://your-app.onrender.com/diagnose  (header X-Diagnose-Token: <DIAGNOSE_SECRET>)

hoặc

    python diagnose_router9.py

``main(out)`` ghi kết quả vào stream ``out`` được truyền vào (web.py dùng
io.StringIO riêng cho mỗi request) thay vì ``print`` ra stdout chung - tránh
phải ``redirect_stdout`` toàn process trong lúc server async đang chờ mạng.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from typing import TextIO

from ai import orchestrator, router9_client
from core import config

LINE = "=" * 70


def title(text: str, out: TextIO) -> None:
    print(file=out)
    print(LINE, file=out)
    print(text, file=out)
    print(LINE, file=out)


async def main(out: TextIO | None = None) -> None:
    out = out if out is not None else sys.stdout

    def emit(*args: object) -> None:
        print(*args, file=out)

    title("THÔNG TIN MÔI TRƯỜNG", out)

    emit(f"ROUTER9_BASE_URL : {config.ROUTER9_BASE_URL}")
    emit(f"ROUTER9_MODEL    : {config.ROUTER9_MODEL}")
    emit(f"ROUTER9_API_KEY  : {'OK' if config.ROUTER9_API_KEY else 'MISSING'}")
    emit()

    if not config.ROUTER9_API_KEY:
        emit("Không có API key 9Router.")
        return

    title("LIST MODELS", out)

    try:
        models = await router9_client.list_models()
        if not models:
            emit("Không lấy được danh sách model (gateway có thể không hỗ trợ /models).")
        else:
            emit(f"Số model: {len(models)}")
            for model in models:
                emit(" -", model)
    except Exception:
        traceback.print_exc(file=out)

    title("TEST TEXT", out)

    prompt = "Trả lời ngắn gọn 1 câu: bạn đang hoạt động bình thường chứ?"

    try:
        response = await orchestrator.ask(prompt)

        emit("Response class:", type(response))
        emit()

        text = getattr(response, "text", "")

        emit("TEXT")
        emit("--------------------------------")

        if text:
            emit(text)
        else:
            emit("(empty)")

    except Exception:
        traceback.print_exc(file=out)

    title("DONE", out)


if __name__ == "__main__":
    asyncio.run(main())
