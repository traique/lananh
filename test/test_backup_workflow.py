"""Bất biến của workflow sao lưu (.github/workflows/backup.yml) - repo PUBLIC nên
artifact/log ai cũng xem được: bỏ qua êm khi chưa có secret, mã hoá TRƯỚC khi
upload, không in dữ liệu, chỉ schema public qua role chỉ-đọc, giờ chạy tránh
khung VACUUM FULL."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/backup.yml"


def _wf():
    return yaml.safe_load(WORKFLOW.read_text("utf-8"))


def _steps(job):
    return _wf()["jobs"][job]["steps"]


def test_chi_chay_theo_lich_va_tay_khong_chay_cho_pr():
    triggers = _wf()[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}  # PR từ fork không chạm secret
    crons = [item["cron"] for item in triggers["schedule"]]
    assert "47 21 * * *" in crons
    # VACUUM FULL: Chủ nhật 03:00-03:59 giờ VN = thứ 7 20:00-20:59 UTC.
    for cron in crons:
        minute, hour = cron.split()[:2]
        assert hour != "20", cron
    assert _wf()["permissions"] == {"contents": "read"}


def test_bo_qua_em_khi_chua_cau_hinh_secret():
    steps = _steps("backup")
    first = steps[0]
    assert first["id"] == "cfg"
    assert 'echo "configured=false"' in first["run"] and "exit 0" in first["run"]
    for step in steps[1:]:
        if step.get("name") == "Dọn file tạm":
            continue
        assert step.get("if") == "steps.cfg.outputs.configured == 'true'", step.get("name")
    drill_if = _wf()["jobs"]["restore-drill"]["if"]
    assert "needs.backup.outputs.configured == 'true'" in drill_if


def test_ma_hoa_truoc_khi_upload_va_khong_in_du_lieu():
    steps = _steps("backup")
    names = [s.get("name") or s.get("uses") for s in steps]
    dump = steps[names.index("Dump + mã hoá")]["run"]
    upload_index = next(i for i, s in enumerate(steps) if "upload-artifact" in str(s.get("uses")))
    assert names.index("Dump + mã hoá") < upload_index
    # pg_dump chảy thẳng vào gpg qua pipe, không ghi file bản rõ.
    assert "| gpg" in dump and "--symmetric --cipher-algo AES256" in dump
    assert "--passphrase-file <(printf" in dump  # passphrase không nằm trên dòng lệnh
    upload = steps[upload_index]["with"]
    assert upload["path"].endswith("*.gpg") and upload["if-no-files-found"] == "error"
    text = WORKFLOW.read_text("utf-8")
    assert "set -x" not in text and "cat " not in text and "pg_restore --list" in text
    # Chỉ schema public, không owner/privileges, bỏ dữ liệu bảng lớn/tạm; giữ prompts.
    assert "--schema=public --no-owner --no-privileges" in dump
    excluded = _wf()["env"]["EXCLUDE_TABLE_DATA"].split()
    assert {"facebook_post_media", "webhook_inbox", "user_calls", "provider_calls"} <= set(excluded)
    assert "prompts" not in excluded and "results" not in excluded


def test_url_db_chi_mo_rong_ben_trong_container():
    """URL (có mật khẩu) chỉ được mở rộng trong chuỗi nháy đơn của ``sh -c`` chạy
    TRONG container, không bao giờ nằm trên dòng lệnh docker/host."""
    uses = 0
    for job in ("backup", "restore-drill"):
        for step in _steps(job):
            run = step.get("run", "")
            uses += run.count('--dbname="$BACKUP_DATABASE_URL"')
            outside_quotes = re.sub(r"'[^']*'", "''", run)
            outside_quotes = outside_quotes.replace('[ -z "$BACKUP_DATABASE_URL" ]', "")
            assert not re.search(r"\$\{?BACKUP_DATABASE_URL", outside_quotes), step.get("name")
    assert uses >= 4  # dump, đếm dòng, 2 lần ghi nhịp tim


def test_dien_tap_khoi_phuc_hang_thang():
    job = _wf()["jobs"]["restore-drill"]
    assert "17 22 1 * *" in job["if"] and "inputs.drill" in job["if"]
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "python -m scripts.backup_drill" in runs and "--exit-on-error" in runs
    assert any(step.get("if") == "always()" for step in job["steps"])  # dọn file đã giải mã


def test_tai_lieu_va_role_chi_doc():
    doc = (ROOT / "docs/backup-restore.md").read_text("utf-8")
    assert "CREATE ROLE lananh_backup" in doc and "GRANT INSERT ON backup_runs" in doc
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA public" in doc
    assert "SETTINGS_ENC_KEY" in doc and "Session" in doc
    migration = (ROOT / "migrations/023_backup_runs.sql").read_text("utf-8")
    assert "PASSWORD" not in migration.upper().replace("MẬT KHẨU", "")  # không có secret
