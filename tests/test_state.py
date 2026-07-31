"""§7 状態ファイル。"""
import json

from src.state import State, today_jst


def test_seen_dedupes_per_source(tmp_path):
    s = State(data_dir=tmp_path)
    assert not s.is_seen("那覇市", "https://x.jp/1")
    s.mark_seen("那覇市", "https://x.jp/1")
    assert s.is_seen("那覇市", "https://x.jp/1")
    # 機関が違えば別扱い
    assert not s.is_seen("沖縄県", "https://x.jp/1")


def test_seen_rotates_at_limit(tmp_path):
    s = State(data_dir=tmp_path)
    for i in range(120):
        s.mark_seen("那覇市", f"https://x.jp/{i}")
    s.save(seen_limit=100)
    saved = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert len(saved["那覇市"]) == 100
    assert saved["那覇市"][0] == "https://x.jp/20"   # 古い方から捨てる
    assert saved["那覇市"][-1] == "https://x.jp/119"


def test_quota_rolls_over_on_new_day(tmp_path):
    (tmp_path / "quota.json").write_text(
        json.dumps({"date": "2020-01-01", "count": 55}), encoding="utf-8"
    )
    s = State(data_dir=tmp_path)
    assert s.quota_count == 0
    assert s.quota["date"] == today_jst()


def test_quota_persists_within_day(tmp_path):
    (tmp_path / "quota.json").write_text(
        json.dumps({"date": today_jst(), "count": 12}), encoding="utf-8"
    )
    s = State(data_dir=tmp_path)
    assert s.quota_count == 12
    assert s.quota_remaining(60) == 48
    s.consume_quota(3)
    assert s.quota_count == 15


def test_results_dedupe_by_url(tmp_path):
    s = State(data_dir=tmp_path)
    assert s.add_result({"url": "https://x.jp/a", "recorded_at": "2026-07-01"})
    assert not s.add_result({"url": "https://x.jp/a", "recorded_at": "2026-07-02"})
    assert len(s.results) == 1


def test_results_in_month(tmp_path):
    s = State(data_dir=tmp_path)
    s.add_result({"url": "a", "recorded_at": "2026-07-31T10:00:00+09:00"})
    s.add_result({"url": "b", "recorded_at": "2026-08-01T10:00:00+09:00"})
    assert len(s.results_in_month(2026, 7)) == 1
    assert len(s.results_in_month(2026, 8)) == 1


def test_dry_run_does_not_write(tmp_path):
    s = State(data_dir=tmp_path, dry_run=True)
    s.mark_seen("那覇市", "https://x.jp/1")
    s.save()
    assert not (tmp_path / "seen.json").exists()


def test_roundtrip(tmp_path):
    s = State(data_dir=tmp_path)
    s.mark_seen("那覇市", "https://x.jp/1")
    s.set_pending([{"id": "s1", "url": "https://x.jp/2"}])
    s.add_items([{"id": "s1", "rank": "S"}])
    s.consume_quota(2)
    s.save()

    s2 = State(data_dir=tmp_path)
    assert s2.is_seen("那覇市", "https://x.jp/1")
    assert s2.pending[0]["url"] == "https://x.jp/2"
    assert s2.items[0]["rank"] == "S"
    assert s2.quota_count == 2


def test_corrupt_json_falls_back_to_default(tmp_path):
    """壊れた JSON で落とさず継続する。"""
    (tmp_path / "seen.json").write_text("{ broken", encoding="utf-8")
    s = State(data_dir=tmp_path)
    assert s.seen == {}
