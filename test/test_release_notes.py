"""Báo chủ bot (Telegram + Zoom) khi đã lên bản mới sau deploy - services/release_notes."""

import pytest

from core import config, version
from services import release_notes


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Render, chủ bot 777, Zoom tắt, CHANGELOG mẫu 2 bản, settings + hub giả."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(
        "## 1.1.0 - 2026-10-30\n\n- Câu chuyện thường không kéo sang cổ phiếu\n- Có /phienban\n\n"
        "## 1.0.0 - 2026-10-20\n\n- Bản đầu\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(version, "CHANGELOG_PATH", path)
    version.releases.cache_clear()
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc1234ffff")
    monkeypatch.delenv("RELEASE_NOTIFY", raising=False)
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 777)
    monkeypatch.setattr(config, "ZOOM_ENABLED", False)
    monkeypatch.setattr(release_notes, "_attempted", False)
    state = {"settings": {}, "sent": [], "accept": True, "pairing": ("jid-1", "Chủ bot")}

    async def get_setting(key):
        return state["settings"].get(key)

    async def set_setting(key, value):
        state["settings"][key] = value

    async def zoom_pairing():
        return state["pairing"]

    async def send(channel, user_id, text, *, source, level=None, dedup=None):
        state["sent"].append((channel, user_id, text, source, dedup))
        return state["accept"]

    monkeypatch.setattr(release_notes.db, "get_setting", get_setting)
    monkeypatch.setattr(release_notes.db, "set_setting", set_setting)
    monkeypatch.setattr(release_notes.db, "zoom_get_pairing", zoom_pairing)
    monkeypatch.setattr(release_notes.notify_hub, "send", send)
    yield state
    version.releases.cache_clear()


@pytest.mark.asyncio
async def test_ban_moi_bao_telegram_va_zoom_dung_mot_lan(env, monkeypatch):
    monkeypatch.setattr(config, "ZOOM_ENABLED", True)
    env["settings"][release_notes.ANNOUNCED_KEY] = "1.0.0@9999999"

    assert await release_notes.announce_if_new() is True
    assert [item[0] for item in env["sent"]] == ["telegram", "zoom"]
    _, user_id, text, source, dedup = env["sent"][0]
    assert user_id == 777 and source == "release" and dedup == "release:1.1.0@abc1234"
    assert text.startswith("🆕 Em đã lên bản mới 1.1.0 (abc1234) (trước đó 1.0.0).")
    assert "• Câu chuyện thường không kéo sang cổ phiếu" in text and "/phienban" in text
    assert env["settings"][release_notes.ANNOUNCED_KEY] == "1.1.0@abc1234"

    # Vòng giám sát sau trong cùng process, hay process mới cùng commit: không báo lại.
    assert await release_notes.announce_if_new() is False
    monkeypatch.setattr(release_notes, "_attempted", False)
    assert await release_notes.announce_if_new() is False
    assert len(env["sent"]) == 2


@pytest.mark.asyncio
async def test_zoom_tat_hoac_chua_pair_chi_gui_telegram(env, monkeypatch):
    """Gửi "zoom" khi chưa pair thì hub quay về Telegram - chủ bot nhận 2 tin trùng."""
    assert await release_notes.announce_if_new() is True
    monkeypatch.setattr(config, "ZOOM_ENABLED", True)
    env["pairing"] = None
    monkeypatch.setattr(release_notes, "_attempted", False)
    env["settings"].clear()
    assert await release_notes.announce_if_new() is True
    assert [item[0] for item in env["sent"]] == ["telegram", "telegram"]


@pytest.mark.asyncio
async def test_lan_dau_khong_co_moc_van_bao(env):
    assert await release_notes.announce_if_new() is True
    text = env["sent"][0][2]
    assert text.startswith("🆕 Em đã lên bản mới 1.1.0 (abc1234).")


@pytest.mark.asyncio
async def test_commit_moi_cung_phien_ban_bao_deploy_lai(env):
    env["settings"][release_notes.ANNOUNCED_KEY] = "1.1.0@0000000"
    assert await release_notes.announce_if_new() is True
    text = env["sent"][0][2]
    assert text.startswith("🔄 Em vừa được deploy lại bản 1.1.0 (abc1234)")
    assert "Có gì mới" not in text


@pytest.mark.asyncio
async def test_gui_hong_khong_ghi_moc_lan_khoi_dong_sau_thu_lai(env, monkeypatch):
    env["accept"] = False
    assert await release_notes.announce_if_new() is False
    assert release_notes.ANNOUNCED_KEY not in env["settings"]
    # Cùng process không gửi dồn mỗi vòng giám sát.
    env["accept"] = True
    assert await release_notes.announce_if_new() is False
    monkeypatch.setattr(release_notes, "_attempted", False)
    assert await release_notes.announce_if_new() is True
    assert len(env["sent"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup",
    [
        lambda mp: mp.setenv("RENDER", "false"),  # chạy local trỏ DB production
        lambda mp: mp.setenv("RELEASE_NOTIFY", "0"),
        lambda mp: mp.setattr(config, "ALLOWED_USER_ID", 0),
    ],
)
async def test_khong_bao_khi_tat_hoac_khong_tren_render(env, monkeypatch, setup):
    setup(monkeypatch)
    assert await release_notes.announce_if_new() is False
    assert env["sent"] == [] and env["settings"] == {}


@pytest.mark.asyncio
async def test_changelog_hong_van_bao_deploy(env, monkeypatch, tmp_path):
    monkeypatch.setattr(version, "CHANGELOG_PATH", tmp_path / "khong-co.md")
    version.releases.cache_clear()
    assert await release_notes.announce_if_new() is True
    assert "chưa đọc được lịch sử phiên bản" in env["sent"][0][2]
    assert env["settings"][release_notes.ANNOUNCED_KEY] == "?@abc1234"


def test_nguon_release_la_urgent_trong_hub():
    from services import notify_hub

    spec = notify_hub.SOURCES[release_notes.SOURCE]
    assert spec.level == notify_hub.URGENT and not spec.quiet_exempt
