import sys
from pathlib import Path

# Fix import path for tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config


def test_load_chat_skill_template_rendering():
    result = config.load_chat_skill()
    # It should not contain raw YAML keys if rendered properly
    assert "persona:" not in result
    assert "tone_of_voice:" not in result
    # It should contain rendered text (for example, the persona content)
    assert len(result) > 0


def test_owner_profile_only_rendered_for_owner(tmp_path, monkeypatch):
    import yaml

    data = yaml.safe_load(config.CHAT_SKILL_PATH.read_text(encoding="utf-8"))
    data["persona"]["user_profile"]["nickname"] = "Tuấn-bí-mật"
    data["persona"]["user_profile"]["things_to_remember"] = "đang giữ 5000 cp HPG"
    custom = tmp_path / "chat_skill.yaml"
    custom.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(config, "CHAT_SKILL_PATH", custom)
    try:
        owner = config.load_chat_skill(force_reload=True)
        member = config.load_chat_skill(include_owner_profile=False)
        assert "Tuấn-bí-mật" in owner and "5000 cp HPG" in owner
        assert "Tuấn-bí-mật" not in member and "5000 cp HPG" not in member
        assert "KHÔNG phải chủ bot" in member
        # Thành viên không được LLM tự đưa khuyến nghị mua/bán trong chat thường.
        assert "không đưa khuyến nghị mua/bán/giữ" in member
        assert "không đưa khuyến nghị mua/bán/giữ" not in owner
    finally:
        config._chat_skill_cache.clear()


def test_only_allowed_user_id_counts_as_owner(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 777)
    assert config.is_owner_user_id(777)
    assert not config.is_owner_user_id(-1)  # Zalo internal_user_id luôn âm
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 0)
    assert not config.is_owner_user_id(0)
