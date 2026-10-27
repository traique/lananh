"""Tra kết quả xổ số dạng dữ liệu cho web công khai /xo-so và GET /api/v1/lottery.

Dùng lại đúng đường của chat (services/vn_daily_chat.province_draws/pick_province_draw,
vn_market_data.get_lottery + check_ticket): số liệu luôn lấy từ feed xskt.com.vn,
không gọi AI, không đụng DB. Câu chữ ở đây trung tính (trang công khai), không theo
giọng Lan Anh.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from services import lottery_provinces as provinces
from services import vn_daily_chat
from services import vn_market_data as market

SOURCE = "xskt.com.vn"
PRIZE_ORDER = ("ĐB", "1", "2", "3", "4", "5", "6", "7", "8")
PRIZE_LABELS = {"ĐB": "Đặc biệt", **{k: f"Giải {v}" for k, v in zip(
    PRIZE_ORDER[1:], ("Nhất", "Nhì", "Ba", "Tư", "Năm", "Sáu", "Bảy", "Tám"), strict=True
)}}  # fmt: skip
REGION_KEYS = {
    "mn": "mn", "mien-nam": "mn", "xsmn": "mn",
    "mt": "mt", "mien-trung": "mt", "xsmt": "mt",
    "mb": "mb", "mien-bac": "mb", "xsmb": "mb",
}  # fmt: skip


class LookupInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Target:
    kind: str  # "region" | "province"
    key: str  # "mn" | "tay-ninh"
    name: str
    region: str
    province: provinces.Province | None = None


def resolve_target(raw: str) -> Target:
    """ "" -> miền Nam; "mn"/"mien-nam" -> cả miền; khoá/tên đài -> đài đó."""
    text = (raw or "").strip()[:60]
    key = text.lower()
    if not key:
        key = "mn"
    region = REGION_KEYS.get(key)
    if region is None:
        province = provinces.get(text)
        if province is None:
            raise LookupInputError("INVALID_PROVINCE", "Không có đài xổ số này trong danh mục.")
        if province.region == "mb":
            region = "mb"
        else:
            return Target("province", province.key, province.name, province.region, province)
    return Target("region", region, provinces.REGION_LABELS[region], region)


def parse_day(raw: str, today: dt.date) -> dt.date | None:
    text = (raw or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text) or re.fullmatch(
        r"(\d{1,2})/(\d{1,2})/(\d{4})", text
    )
    try:
        if m and len(m.group(1)) == 4:
            day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif m:
            day = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        else:
            raise ValueError
    except ValueError:
        raise LookupInputError("INVALID_DATE", "Ngày có dạng YYYY-MM-DD.") from None
    if day > today:
        raise LookupInputError("INVALID_DATE", "Chưa tới ngày quay này.")
    return day


def parse_ticket(raw: str, region: str) -> str:
    ticket = re.sub(r"\s+", "", raw or "")
    if not ticket:
        return ""
    digits = market.ticket_digits(region)
    if not re.fullmatch(rf"\d{{{digits}}}", ticket):
        where = provinces.REGION_LABELS[region].lower()
        raise LookupInputError("INVALID_TICKET", f"Vé xổ số {where} có {digits} chữ số.")
    return ticket


def _draw_json(draw: market.LotteryDraw) -> dict:
    return {
        "date": draw.date.isoformat(),
        "weekday": provinces.WEEKDAYS[draw.date.weekday()],
        "region": draw.region,
        "link": draw.link,
        "provinces": [
            {
                "name": p.name,
                "prizes": {k: list(p.prizes[k]) for k in PRIZE_ORDER if p.prizes.get(k)},
            }
            for p in draw.provinces
        ],
    }


def _ticket_json(ticket: str, draw: market.LotteryDraw) -> dict:
    results = []
    for p in draw.provinces:
        wins = market.check_ticket(ticket, p.prizes, draw.region)
        results.append(
            {
                "province": p.name,
                "wins": [{"code": w, "name": market.PRIZE_NAMES[w]} for w in wins],
            }
        )
    return {"number": ticket, "results": results}


def _target_json(target: Target, today: dt.date) -> dict:
    out: dict[str, object] = {
        "kind": target.kind,
        "key": target.key,
        "name": target.name,
        "region": target.region,
        "regionName": provinces.REGION_LABELS[target.region],
        "drawTime": provinces.DRAW_TIMES[target.region],
    }
    if target.province is not None:
        out["drawDays"] = [provinces.WEEKDAYS[d] for d in target.province.weekdays]
        out["drawsToday"] = target.province.draws_on(today)
    else:
        out["drawsToday"] = True
    return out


def _note(target: Target, status: str, day: dt.date | None, draw, today: dt.date) -> str:
    if status in ("latest", "exact"):
        return ""
    p = target.province
    when = (
        "Hôm nay"
        if day == today
        else f"{provinces.WEEKDAYS[day.weekday()]} {day:%d/%m/%Y}"
        if day
        else ""
    )
    schedule = (
        f"Đài {p.name} quay {provinces.schedule_text(p)} lúc {provinces.DRAW_TIMES[p.region]}."
        if p is not None
        else ""
    )
    if status == "not_drawn":
        tail = f" Đang hiện kỳ gần nhất trước đó ({draw.date:%d/%m/%Y})." if draw else ""
        return f"{when} đài {p.name if p else ''} không quay. {schedule}{tail}".strip()
    if status == "pending":
        tail = f" Đang hiện kỳ gần nhất ({draw.date:%d/%m/%Y})." if draw else ""
        return (
            f"Kỳ hôm nay quay lúc {provinces.DRAW_TIMES[target.region]}, nguồn chưa có kết quả "
            f"(thường cập nhật sau giờ quay ít phút).{tail}"
        )
    if status == "missing":
        return f"Nguồn không còn giữ kết quả ngày {day:%d/%m/%Y} (chỉ giữ khoảng 9 kỳ gần nhất)."
    return "Nguồn chưa có kỳ quay nào của đài này."


async def lookup(selector: str = "", raw_day: str = "", raw_ticket: str = "") -> dict:
    """Dữ liệu 1 lượt tra. Raise LookupInputError (nhập sai) hoặc MarketDataError
    (nguồn lỗi)."""
    today = vn_daily_chat._today()
    target = resolve_target(selector)
    day = parse_day(raw_day, today)
    ticket = parse_ticket(raw_ticket, target.region)
    available: list[str] = []
    incomplete = None
    if target.province is not None:
        draws = await vn_daily_chat.province_draws(target.province)
        pick = vn_daily_chat.pick_province_draw(target.province, draws, day, today)
        status, draw = pick.status, pick.draw
    else:
        draws = await market.get_lottery(target.region)
        draw = market.pick_draw(draws, day)
        if day is None:
            status = "latest"
        elif draw is not None:
            status = "exact"
        elif day == today:
            status, draw = "pending", draws[0] if draws else None
        else:
            status = "missing"
        if draw is not None:
            incomplete = market.coverage(draw, draws, today)
    available = [d.date.isoformat() for d in draws]
    data: dict = {
        "query": {
            "province": target.key,
            "date": day.isoformat() if day else None,
            "ticket": ticket or None,
        },
        "target": _target_json(target, today),
        "status": status,
        "message": _note(target, status, day, draw, today),
        "draw": _draw_json(draw) if draw is not None else None,
        "availableDates": available,
        "source": SOURCE,
    }
    if incomplete is not None:
        data["incomplete"] = {"have": incomplete[0], "usual": incomplete[1]}
        data["message"] = (
            data["message"] + " " if data["message"] else ""
        ) + f"Kỳ hôm nay nguồn mới có {incomplete[0]}/{incomplete[1]} đài."
    # Dò vé: chỉ trên kỳ đúng đài/ngày. Ngày đó đài không quay thì dò kỳ gần nhất
    # trước đó (ghi rõ trong message); kỳ đang chờ kết quả thì không dò.
    if ticket and draw is not None and status != "pending":
        data["ticket"] = _ticket_json(ticket, draw)
    return data


def selector_groups(today: dt.date) -> list[dict]:
    """Ô chọn miền/đài cho trang /xo-so: mỗi miền 1 nhóm, đài quay hôm nay có ⭐."""
    groups = []
    for region in provinces.REGION_ORDER:
        options = [(region, f"Cả {provinces.REGION_LABELS[region].lower()}", False)]
        if region != "mb":
            for p in provinces.in_region(region):
                days = "/".join(provinces.SHORT_WEEKDAYS[d] for d in p.weekdays)
                options.append((p.key, f"{p.name} ({days})", p.draws_on(today)))
        groups.append({"label": provinces.REGION_LABELS[region], "options": options})
    return groups
