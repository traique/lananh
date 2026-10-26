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


# ─── Persona: đủ mọi mục YAML, không còn dòng giữ chỗ, 3 biến thể ───────────


def _leaf_strings(node) -> list[str]:
    if isinstance(node, str):
        return [" ".join(node.split())]
    if isinstance(node, dict):
        return [text for value in node.values() for text in _leaf_strings(value)]
    if isinstance(node, list):
        return [text for value in node for text in _leaf_strings(value)]
    return []


def _render_all_variants() -> dict[str, str]:
    config._chat_skill_cache.clear()
    try:
        return {
            variant: config.load_chat_skill(variant=variant) for variant in config.PERSONA_VARIANTS
        }
    finally:
        config._chat_skill_cache.clear()


def test_moi_muc_yaml_cap_cao_deu_vao_persona():
    """quirks/energy_and_mood/continuity từng bị bỏ quên từ commit đầu: mọi mục
    cấp cao của chat_skill.yaml phải góp chữ vào persona của MỌI biến thể."""
    import yaml

    data = yaml.safe_load(config.CHAT_SKILL_PATH.read_text(encoding="utf-8"))
    for variant, rendered in _render_all_variants().items():
        flat = " ".join(rendered.split())
        for section, node in data.items():
            if section == "persona" and variant != config.PERSONA_OWNER:
                node = {k: v for k, v in node.items() if k != "user_profile"}
            leaves = [text for text in _leaf_strings(node) if len(text) > 20]
            assert any(text in flat for text in leaves), f"{variant}: thiếu mục {section}"
        assert "NÉT RIÊNG" in rendered and "TÍNH LIÊN TỤC" in rendered
        assert "NHỊP THEO GIỜ GIẤC" in rendered
        # Số liệu chứng khoán vẫn chuẩn bất kể "mood".
        assert "chuẩn xác bất kể" in flat
        assert "things_to_remember" not in rendered


def test_khong_con_dong_gia_tri_giu_cho():
    for variant, rendered in _render_all_variants().items():
        for line in rendered.splitlines():
            assert not line.rstrip().endswith(": ..."), f"{variant}: {line}"
            assert ": vd:" not in line, f"{variant}: {line}"


def test_ho_so_chu_bot_chi_render_muc_da_dien(tmp_path, monkeypatch):
    import yaml

    data = yaml.safe_load(config.CHAT_SKILL_PATH.read_text(encoding="utf-8"))
    data["persona"]["user_profile"].update(
        nickname="Tuấn", occupation_or_interests="...", preferred_style="vd: nói thẳng"
    )
    custom = tmp_path / "chat_skill.yaml"
    custom.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(config, "CHAT_SKILL_PATH", custom)
    try:
        owner = config.load_chat_skill(force_reload=True)
        assert "VỀ ANH" in owner and "- Tên / cách gọi thân mật: Tuấn" in owner
        assert "Công việc / mối quan tâm chính" not in owner
        assert "Phong cách anh thích" not in owner
    finally:
        config._chat_skill_cache.clear()


def test_ho_so_trong_het_thi_bo_ca_khoi_ve_anh():
    owner = _render_all_variants()[config.PERSONA_OWNER]
    # chat_skill.yaml trong repo còn để "..." hết -> không có khối VỀ ANH nào,
    # và cũng không rơi sang khối "thành viên" dành cho Zalo.
    assert "VỀ ANH" not in owner
    assert "VỀ NGƯỜI ĐANG TRÒ CHUYỆN" not in owner


def test_bien_the_admin_zalo_khong_ho_so_khong_bi_goi_la_nguoi_ngoai(tmp_path, monkeypatch):
    import yaml

    data = yaml.safe_load(config.CHAT_SKILL_PATH.read_text(encoding="utf-8"))
    data["persona"]["user_profile"]["nickname"] = "Tuấn-bí-mật"
    custom = tmp_path / "chat_skill.yaml"
    custom.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(config, "CHAT_SKILL_PATH", custom)
    try:
        admin = config.load_chat_skill(force_reload=True, variant=config.PERSONA_ZALO_ADMIN)
        member = config.load_chat_skill(variant=config.PERSONA_MEMBER)
        assert "Tuấn-bí-mật" not in admin and "Tuấn-bí-mật" not in member
        # Admin có thể là người khác chủ bot: không khẳng định là/không là chủ bot.
        assert "KHÔNG phải chủ bot" not in admin and "quản trị" in admin
        assert "không đưa khuyến nghị mua/bán/giữ" not in admin
        assert "chỉ đến từ báo cáo của hệ thống" in " ".join(admin.split())
        # Cả 2 biến thể Zalo đều được dùng trí nhớ riêng của chính người đó.
        assert "trí nhớ riêng của chính" in " ".join(admin.split())
        assert "trí nhớ riêng của chính" in " ".join(member.split())
        # Biến thể lạ không bao giờ rơi về persona chủ bot.
        odd = config.load_chat_skill(variant="khong-ro")
        assert "Tuấn-bí-mật" not in odd and odd == member
    finally:
        config._chat_skill_cache.clear()


def test_goi_y_kenh_noi_sau_persona_va_cache_co_tran():
    config._chat_skill_cache.clear()
    try:
        plain = config.load_chat_skill(variant=config.PERSONA_MEMBER)
        zalo = config.load_chat_skill(variant=config.PERSONA_MEMBER, channel="zalo")
        telegram = config.load_chat_skill(channel="telegram")
        assert zalo.startswith(plain) and "KÊNH ĐANG TRẢ LỜI: Zalo" in zalo
        assert "không dùng bảng" in zalo
        assert "KÊNH ĐANG TRẢ LỜI: Telegram" in telegram
        assert "KÊNH ĐANG TRẢ LỜI" not in plain
        assert config.load_chat_skill(channel="kenh-la") == config.load_chat_skill()
        for variant in config.PERSONA_VARIANTS:
            for channel in ("zalo", "telegram", "zoom", None):
                config.load_chat_skill(variant=variant, channel=channel)
        assert len(config._chat_skill_cache) <= len(config.PERSONA_VARIANTS)
    finally:
        config._chat_skill_cache.clear()
