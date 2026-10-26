"""Báo trước lịch sự kiện quyền của mã đang giữ (cổ tức, quyền mua, ĐHCĐ)."""

from datetime import date

import pytest

from services import portfolio_events as pe
from stock import portfolio, vci_direct

TODAY = date(2026, 10, 26)  # thứ Hai


def test_parse_events_do_truong_theo_tu_khoa_va_epoch_ms():
    rows = [
        {
            "eventCode": "DIV",
            "eventTitleVi": "VNM - Trả cổ tức đợt 1/2026 bằng tiền",
            "exrightDate": 1793577600000,  # 2026-11-02 00:00 UTC
            "recordDate": "2026-11-03T00:00:00",
            "valuePerShare": 0.15,
        },
        {"eventCode": "AGME", "eventNameVi": "ĐHCĐ", "recordDate": "2026-11-05"},
        {"eventCode": "ISS", "eventTitleVi": "không có ngày"},
        "rác",
    ]
    events = vci_direct.parse_upcoming_events("VNM", rows)
    assert [(e.code, e.key_date) for e in events] == [("DIV", "2026-11-02"), ("AGME", "2026-11-05")]
    assert events[0].record_date == "2026-11-03" and events[0].value == 0.15


def test_fetch_upcoming_events_cua_so_rong_loc_tai_cho_va_cache(monkeypatch):
    calls = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "content": [
                        {"eventCode": "DIV", "exrightDate": "2030-01-02", "valuePerShare": 1000},
                        {"eventCode": "DIV", "exrightDate": "2000-01-02", "valuePerShare": 1000},
                    ]
                }
            }

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        return _Resp()

    monkeypatch.setattr(vci_direct.requests, "get", fake_get)
    monkeypatch.setattr(vci_direct, "_events_cache", {})
    events = vci_direct.fetch_upcoming_events("vnm", days_ahead=365 * 10)
    assert [e.key_date for e in events] == ["2030-01-02"]  # sự kiện quá khứ bị lọc
    assert calls[0]["eventCode"] == "DIV,ISS,AGME,AGMR,EGME" and calls[0]["ticker"] == "VNM"
    assert calls[0]["fromDate"] < calls[0]["toDate"]
    vci_direct.fetch_upcoming_events("VNM", days_ahead=365 * 10)
    assert len(calls) == 1  # cache 12 giờ


def _event(code="DIV", key="2026-10-28", record="", title="", value=None, ex=True):
    return vci_direct.UpcomingEvent(
        "VNM", code, title, key if ex else "", record or ("" if ex else key), "", value
    )


def test_due_events_trong_3_phien_toi():
    events = [
        _event(key="2026-10-26"),
        _event(key="2026-10-29"),  # 3 phiên nữa
        _event(key="2026-10-30"),  # 4 phiên nữa
        _event(key="2026-10-23"),  # đã qua
    ]
    assert [e.key_date for e in pe.due_events(events, TODAY)] == ["2026-10-26", "2026-10-29"]


def test_describe_event_cac_loai():
    div = pe.describe_event(_event(value=0.15, record="2026-10-29"), TODAY)
    assert "GDKHQ cổ tức tiền 1.500đ/cp ngày 28/10 (2 phiên nữa)" in div
    assert "chốt danh sách 29/10" in div and "không phải lỗ thật" in div
    rights = pe.describe_event(_event("ISS", title="Phát hành quyền mua tỷ lệ 10:1"), TODAY)
    assert "quyền mua cổ phiếu phát hành thêm" in rights and "Phát hành quyền mua" in rights
    agm = pe.describe_event(_event("AGME", key="2026-10-26", ex=False), TODAY)
    assert "chốt danh sách quyền dự họp ĐHCĐ thường niên HÔM NAY" in agm


@pytest.mark.asyncio
async def test_gom_mot_tin_moi_nguoi_moi_su_kien_mot_lan(monkeypatch):
    events = {
        "VNM": [_event(key="2026-10-28", value=1500)],
        "FPT": [vci_direct.UpcomingEvent("FPT", "AGME", "", "", "2026-10-27", "", None)],
    }
    logged: set[tuple] = {(-5, "event", "VNM:DIV:2026-10-28")}  # thành viên đã nhận trước đó
    delivered = []

    async def fake_fetch(symbol):
        return events.get(symbol, [])

    async def already(user_id, kind, ref, *, on=None):
        return (user_id, kind, ref) in logged

    async def deliver(user_id, text, *, kind, ref_key, today):
        delivered.append((user_id, text))
        logged.add((user_id, kind, ref_key))
        return True

    async def mark(user_id, kind, ref, today):
        logged.add((user_id, kind, ref))

    monkeypatch.setattr(pe, "_fetch_events", fake_fetch)
    monkeypatch.setattr(pe.portfolio_push, "already_sent", already)
    monkeypatch.setattr(pe.portfolio_push, "deliver", deliver)
    monkeypatch.setattr(pe.portfolio_push, "mark_sent", mark)
    holdings = [
        portfolio.Holding(42, "VNM", 100, 60_000),
        portfolio.Holding(42, "FPT", 10, 100_000),
        portfolio.Holding(-5, "VNM", 50, 60_000),
    ]
    assert await pe.send_event_notices(holdings, TODAY) == 1
    (user_id, text) = delivered[0]
    assert user_id == 42 and "VNM" in text and "FPT" in text
    assert (42, pe.GROUPED_EVENT_KIND, "FPT:AGME:2026-10-27") in logged
    # Lần chạy sau (hôm sau/restart): không gửi lại.
    assert await pe.send_event_notices(holdings, TODAY) == 0


def test_epoch_mili_giay_doi_theo_gio_vn():
    assert vci_direct._event_date(1793577600000) == "2026-11-02"  # 0h UTC
    assert vci_direct._event_date(1793552400000) == "2026-11-02"  # 0h giờ VN
    assert vci_direct._event_date("2026-11-02T07:00:00+07:00") == "2026-11-02"
    assert vci_direct._event_date("rác") == ""
