"""スロット判定（GitHub schedule 欠落対策の gate）。

実測: GitHub は20分おきを頼んでも1日2回程度しか起動せず時刻もバラバラ。
「来た起動を絶対に捨てない」ことを担保する。
"""
import json
from datetime import datetime

from src.slots import JST, due_slot, load_last_batch, mark_done, passed_boundaries


def at(day, h, m):
    return datetime(2026, 8, day, h, m, tzinfo=JST)


# --- 通常のスロット消化 ---


def test_morning_due_when_unrun():
    assert due_slot(at(4, 7, 40), {}) == ("morning", "2026-08-04")


def test_evening_due_after_1737():
    last = {"morning": "2026-08-04"}
    assert due_slot(at(4, 18, 0), last) == ("evening", "2026-08-04")


def test_nothing_due_when_all_done():
    last = {"morning": "2026-08-04", "evening": "2026-08-04"}
    assert due_slot(at(4, 21, 0), last) is None


def test_morning_not_repeated_same_day():
    last = {"morning": "2026-08-04", "evening": "2026-08-03"}
    assert due_slot(at(4, 12, 0), last) is None


# --- 本題: 深夜の起動を捨てない（今回の不具合） ---


def test_night_tick_runs_yesterdays_evening():
    """深夜01:42の起動でも、前日の夕方分が未実行ならそれを消化する。

    実際に 08/04 01:42 と 05:46 の起動を捨ててメールが飛ばなかった。
    """
    last = {"morning": "2026-08-03"}   # 前日の夕方は未実行
    assert due_slot(at(4, 1, 42), last) == ("evening", "2026-08-03")
    assert due_slot(at(4, 5, 46), last) == ("evening", "2026-08-03")


def test_night_tick_idle_when_yesterday_complete():
    last = {"morning": "2026-08-03", "evening": "2026-08-03"}
    assert due_slot(at(4, 1, 42), last) is None


def test_latest_missed_slot_wins():
    """複数溜まっていたら最新の境界から消化する。"""
    assert due_slot(at(4, 20, 0), {}) == ("evening", "2026-08-04")


# --- 一度走れば溜まっていた分はまとめて既済 ---


def test_mark_done_clears_backlog():
    last = {}
    mark_done(at(4, 20, 0), last)
    assert last["evening"] == "2026-08-04"
    assert last["morning"] == "2026-08-04"
    assert due_slot(at(4, 20, 5), last) is None


def test_mark_done_at_night_marks_yesterday_evening():
    last = {"morning": "2026-08-03"}
    mark_done(at(4, 1, 42), last)
    assert last["evening"] == "2026-08-03"
    # 当日の朝が来たら再び実行対象になる
    assert due_slot(at(4, 8, 0), last) == ("morning", "2026-08-04")


def test_mark_done_never_moves_backwards():
    last = {"evening": "2026-08-04"}
    mark_done(at(4, 1, 42), last)
    assert last["evening"] == "2026-08-04"


def test_full_day_cycle_produces_two_mails():
    """前日を消化済みなら、1日に走るのは morning と evening の2回だけ。

    20分おきに起動されても3回目は走らない＝メールは1日2通のまま。
    """
    last = {"morning": "2026-08-03", "evening": "2026-08-03"}
    runs = []
    for hour in range(0, 24):
        for minute in (7, 27, 47):     # 実際の cron と同じ間隔で叩く
            now = at(4, hour, minute)
            if due_slot(now, last):
                runs.append(f"{hour:02d}:{minute:02d}")
                mark_done(now, last)
    assert runs == ["07:47", "17:47"], f"1日2回のはずが {runs}"


def test_cold_start_also_flushes_yesterdays_evening():
    """記録が空なら前日の夕方分も1回だけ消化する（取りこぼし回収）。"""
    last, runs = {}, []
    for hour in range(0, 24):
        now = at(4, hour, 47)
        if due_slot(now, last):
            runs.append(hour)
            mark_done(now, last)
    assert runs == [0, 7, 17], f"前日回収+当日2回のはずが {runs}"


# --- 境界と読み込み ---


def test_boundaries_are_newest_first():
    b = passed_boundaries(at(4, 20, 0))
    assert b[0] == ("evening", "2026-08-04")
    assert b[1] == ("morning", "2026-08-04")


def test_before_first_boundary_of_window():
    """朝07:36は当日朝がまだ来ていないので、前日の夕方が対象。"""
    assert due_slot(at(4, 7, 36), {"evening": "2026-08-03"}) == (
        "morning", "2026-08-03")


def test_load_last_batch_missing_or_broken(tmp_path):
    assert load_last_batch(tmp_path / "none.json") == {}
    p = tmp_path / "b.json"
    p.write_text("{broken", encoding="utf-8")
    assert load_last_batch(p) == {}
    p.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert load_last_batch(p) == {}


def test_module_is_stdlib_only():
    """gate は pip install 前に走るため、外部依存を持ってはいけない。"""
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
