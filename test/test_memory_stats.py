from services import memory_stats


def test_snapshot_has_core_fields_and_never_raises():
    snap = memory_stats.snapshot()
    assert set(snap) == {"python", "container", "processes", "heavy_modules_loaded"}
    assert snap["python"]["rss_mib"] is None or snap["python"]["rss_mib"] > 0
    assert any(p["name"] == "web (python)" for p in snap["processes"]) or not snap["processes"]


def test_unlimited_cgroup_value_is_reported_as_none():
    assert memory_stats._bytes_to_mib(str(1 << 62)) is None
    assert memory_stats._bytes_to_mib(str(512 * 1024 * 1024)) == 512.0
    assert memory_stats._bytes_to_mib("max") is None
