"""スロット判定（GitHub schedule 欠落対策の gate）。"""
import json
from datetime import datetime

from src.slots import JST, current_slot, due_slot, load_last_batch


def at(h, m):
    return datetime(2026, 8, 3, h, m, tzinfo=JST)


# --- current_slot の境界 ---


def test_before_morning_is_none():
    assert current_slot(at(7, 36)) is None
    assert current_slot(at(0, 10)) is None


def test_morning_window():
    assert current_slot(at(7, 37)) == "morning"
    assert current_slot(at(12, 0)) == "morning"
    assert current_slot(at(17, 36)) == "morning"


def test_evening_window():
    assert current_slot(at(17, 37)) == "evening"
    assert current_slot(at(23, 59)) == "evening"


# --- due_slot（実行済み記録との突き合わせ） ---


def test_due_when_not_yet_run():
    assert due_slot(at(7, 40), {}) == "morning"
    assert due_slot(at(18, 0), {"morning": "2026-08-03"}) == "evening"


def test_not_due_when_already_run_today():
    assert due_slot(at(9, 0), {"morning": "2026-08-03"}) is None
    assert due_slot(at(21, 0), {"evening": "2026-08-03"}) is None


def test_due_when_marker_is_from_yesterday():
    assert due_slot(at(7, 40), {"morning": "2026-08-02"}) == "morning"


def test_delayed_tick_still_runs():
    """GitHubが数時間遅れて起動しても、そのスロットが未実行なら走る。"""
    assert due_slot(at(16, 41), {}) == "morning"
    assert due_slot(at(21, 16), {"morning": "2026-08-03"}) == "evening"


def test_night_ticks_do_nothing():
    assert due_slot(at(3, 7), {}) is None


# --- 記録の読み込み ---


def test_load_last_batch_missing_file(tmp_path):
    assert load_last_batch(tmp_path / "none.json") == {}


def test_load_last_batch_broken_file(tmp_path):
    p = tmp_path / "b.json"
    p.write_text("{broken", encoding="utf-8")
    assert load_last_batch(p) == {}


def test_load_last_batch_roundtrip(tmp_path):
    p = tmp_path / "last_batch.json"
    p.write_text(json.dumps({"morning": "2026-08-03"}), encoding="utf-8")
    assert load_last_batch(p) == {"morning": "2026-08-03"}


def test_module_is_stdlib_only():
    """gate は pip install 前に動くため、外部依存を持ってはいけない。"""
    import ast
    from pathlib import Path

    src = (Path("src") / "slots.py").read_text(encoding="utf-8")
    allowed = {"json", "datetime", "pathlib", "__future__"}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            continue
        assert names <= allowed, f"外部依存が混入: {names - allowed}"
