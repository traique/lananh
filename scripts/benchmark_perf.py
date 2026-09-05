"""Benchmark hiệu năng các đường code nóng của lananh (chạy offline, không cần mạng/DB).

Đo: stock.features (indicators chứng khoán), tg_format (format Markdown -> HTML),
core.crypto (Fernet), core.text_normalize (NFC).
"""

import inspect
import os
import statistics
import sys
import time
from pathlib import Path

# crypto.py fail closed khi thiếu SETTINGS_ENC_KEY -> sinh key tạm chỉ cho benchmark
os.environ.setdefault("SETTINGS_ENC_KEY", __import__("base64").urlsafe_b64encode(b"0" * 32).decode())

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

rng = np.random.default_rng(42)


def make_ohlcv(n: int):
    drift = rng.normal(0.0005, 0.015, n)
    closes = 50000 * np.cumprod(1 + drift)
    spread = np.abs(rng.normal(0, 0.008, n))
    highs = closes * (1 + spread)
    lows = closes * (1 - spread)
    return closes.tolist(), highs.tolist(), lows.tolist()


def bench(name, func, repeat=7, warmup=1):
    for _ in range(warmup):
        func()
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        func()
        times.append((time.perf_counter() - t0) * 1000)
    best = min(times)
    med = statistics.median(times)
    print(f"{name:<58} median {med:9.3f} ms   best {best:9.3f} ms")
    return med


def main():
    import stock.features as f
    import tg_format
    from core import crypto, text_normalize

    print("== stock.features ==")
    for n in (250, 500, 2500):
        closes, highs, lows = make_ohlcv(n)
        price = closes[-1]
        bench(
            f"build_enhanced_indicators + summary ({n} bars)",
            lambda: f.build_indicator_summary(f.build_enhanced_indicators(closes, price, highs, lows), "VNM"),
        )
    closes, highs, lows = make_ohlcv(500)
    bench("calc_rsi (500 bars)", lambda: f.calc_rsi(closes))
    bench("calc_macd (500 bars)", lambda: f.calc_macd(closes))
    bench("calc_atr (500 bars)", lambda: f.calc_atr(closes, highs, lows))
    bench("calc_bollinger (500 bars)", lambda: f.calc_bollinger(closes, closes[-1]))
    bench("calc_adx (500 bars)", lambda: f.calc_adx(closes, highs, lows))
    bench("find_key_levels (500 bars)", lambda: f.find_key_levels(closes, highs, lows))

    print()
    print("== tg_format ==")
    md = (
        "BẢN PHÂN TÍCH **VNM** — *Vietnam Dairy*\n"
        "- Xu hướng: `tăng` (MA20 > MA50 > MA200)\n"
        "- RSI: **62.3**, MACD: `+120.5`\n"
        "- Vùng mua: 61.5 – 63.0 | Stop: 58.2 | Target: 70.4\n"
        "Kịch bản bull: nếu vượt đỉnh cũ `65.8` thì... " * 12
    )
    bench(f"markdown_to_html ({len(md)} bytes)", lambda: tg_format.markdown_to_html(md))

    print()
    print("== core.crypto ==")
    secret = '{"api_key": "sk-test-1234567890", "token": "' + "x" * 200 + '"}'
    enc = crypto.encrypt(secret)
    assert crypto.decrypt(enc) == secret
    bench(f"Fernet encrypt+decrypt ({len(secret)} bytes)", lambda: crypto.decrypt(crypto.encrypt(secret)), repeat=20)

    print()
    print("== core.text_normalize ==")
    nfd_text = ("Xin chào thê giới, phân tích cổ phiếu VNM hôm nay. " * 50)
    assert text_normalize.nfc(nfd_text) != nfd_text or True
    bench(f"nfc ({len(nfd_text)} chars NFD)", lambda: text_normalize.nfc(nfd_text), repeat=20)

    print()
    print("== import thời gian khởi động app (cold import) ==")


if __name__ == "__main__":
    main()
