"""Helper xử lý chuỗi nhỏ dùng chung giữa handlers/services/ai.

Tách riêng (không import gì ngoài stdlib) để mọi tầng gọi được mà không kéo
theo vòng import.
"""

# Tham số bật/tắt dùng chung cho /router9, /tavily, /anh, /memory trên mọi kênh.
ON_ARGS = frozenset({"on", "bat", "bật"})
OFF_ARGS = frozenset({"off", "tat", "tắt"})


def parse_on_off(arg: str) -> bool | None:
    """True = bật, False = tắt, None = không phải tham số bật/tắt.

    Chỉ strip + lower, KHÔNG chuẩn hoá NFC - caller tự quyết (vd
    handlers.common.extract_arg đã nfc sẵn)."""
    lowered = (arg or "").strip().lower()
    if lowered in ON_ARGS:
        return True
    if lowered in OFF_ARGS:
        return False
    return None


def strip_json_fence(text: str | None) -> str:
    """Bỏ code fence ```json ... ``` model hay bọc quanh JSON, trả chuỗi sẵn
    sàng cho json.loads(). Text không bắt đầu bằng ``` chỉ được strip()."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
