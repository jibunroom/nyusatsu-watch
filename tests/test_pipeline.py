"""パイプライン全体をネット・Gemini・SMTP 抜きで通す（§11-1・§11-3）。

fetch を差し替え、fixtures のフィードだけで main.run を最後まで走らせる。
"""
import json
from datetime import date

import pytest

from src import config, main as main_mod
from src.fetch import FeedItem, parse_feed
from src.judge import Judge
from src.prefilter import Prefilter
from src.rank import rank_all
from src.state import State
from tests.conftest import read_fixture, read_fixture_bytes

FEEDS = {
    "沖縄県": "feed_okinawa_pref.xml",
    "那覇市": "feed_naha.xml",
    "宜野湾市": "feed_ginowan.rdf",
    "沖縄市": "feed_okinawa_city.xml",
}


class FakeFetcher:
    """ネットに出ない Fetcher の代役。"""

    def __init__(self):
        self.detail_calls = 0

    def get(self, url, check_robots=True):
        raise AssertionError("テスト中にネットへ出ようとした: " + url)


def fake_fetch_source(fetcher, source):
    name = source["name"]
    if name not in FEEDS:
        return [], False
    return parse_feed(read_fixture_bytes(FEEDS[name]), name), True


def fake_fetch_detail(fetcher, item, max_chars):
    fetcher.detail_calls += 1
    item.body = ("案件名: " + item.title + "\n募集期間 2026年8月1日から")[:max_chars]
    return True


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(main_mod, "fetch_source", fake_fetch_source)
    monkeypatch.setattr(main_mod, "fetch_detail", fake_fetch_detail)
    monkeypatch.setattr(main_mod, "Fetcher", lambda *a, **k: FakeFetcher())
    monkeypatch.setattr(main_mod, "git_persist", lambda dry_run: None)
    return True


class Args:
    dry_run = True
    limit = None
    no_ai = True
    verbose = False


def test_full_pipeline_dry_run_no_network(offline, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "State",
                        lambda **kw: State(data_dir=tmp_path, dry_run=True))
    rc = main_mod.run(Args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "--- 実行サマリ ---" in out
    assert "Subject:" in out
    assert "巡回: 4/" in out, "fixtures のある4機関は成功するはず"


def test_pipeline_sends_mail_even_with_zero_hits(monkeypatch, capsys, tmp_path):
    """生存通知（§5-2）。新着ゼロでも必ず送る。"""
    monkeypatch.setattr(main_mod, "fetch_source", lambda f, s: ([], False))
    monkeypatch.setattr(main_mod, "Fetcher", lambda *a, **k: FakeFetcher())
    monkeypatch.setattr(main_mod, "git_persist", lambda dry_run: None)
    monkeypatch.setattr(main_mod, "State",
                        lambda **kw: State(data_dir=tmp_path, dry_run=True))
    main_mod.run(Args())
    out = capsys.readouterr().out
    assert "[入札] 本日0件・巡回" in out


def test_second_run_sees_nothing_new(offline, monkeypatch, tmp_path, capsys):
    """seen.json による重複排除（§7）。"""
    state = State(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "State", lambda **kw: state)
    monkeypatch.setattr(state, "dry_run", False)

    main_mod.run(Args())
    first = capsys.readouterr().out
    assert "新着: 0件" not in first

    main_mod.run(Args())
    second = capsys.readouterr().out
    assert "新着: 0件" in second


def test_limit_caps_items_sent_to_gemini(offline, monkeypatch, tmp_path, capsys):
    """§11-4 --limit 1 で Gemini に渡るのは1件だけ。"""
    monkeypatch.setattr(main_mod, "State",
                        lambda **kw: State(data_dir=tmp_path, dry_run=True))
    seen_batch = []

    class Args1(Args):
        limit = 1
        no_ai = False

    def fake_caller_builder(settings, args):
        def caller(prompt):
            seen_batch.append(prompt.count("id: s"))
            return json.dumps([{"id": "s1", "relevant": True, "reason": "テスト"}])
        return caller

    monkeypatch.setattr(main_mod, "_build_caller", fake_caller_builder)
    main_mod.run(Args1())
    assert seen_batch == [1], f"1件だけのはずが {seen_batch}"


def test_pending_carried_over_between_runs(offline, monkeypatch, tmp_path, capsys):
    """上限到達で持ち越し、次回はまず pending から消化する（§4-1）。"""
    state = State(data_dir=tmp_path)
    state.consume_quota(59)   # 残り1リクエスト
    monkeypatch.setattr(main_mod, "State", lambda **kw: state)

    calls = []

    def fake_caller_builder(settings, args):
        def caller(prompt):
            ids = [ln.split("id: ")[1].split("\n")[0]
                   for ln in prompt.split("---\n")[1:]]
            calls.append(ids)
            return json.dumps([{"id": i, "relevant": False} for i in ids])
        return caller

    monkeypatch.setattr(main_mod, "_build_caller", fake_caller_builder)

    class ArgsAI(Args):
        no_ai = False

    main_mod.run(ArgsAI())
    out = capsys.readouterr().out
    assert len(calls) == 1, "残り1リクエストしか使えない"
    assert "次回持ち越し:" in out
    assert len(state.pending) > 0
    # 持ち越した件は本文取得済みなので再取得しない
    assert all(p["body"] for p in state.pending)


def test_error_mail_on_exception(monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("意図的な失敗")

    monkeypatch.setattr(main_mod, "run", boom)
    rc = main_mod.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[入札] 実行エラー" in out
    assert "意図的な失敗" in out


# --- 段ごとの結合（実データ） ---


def test_stages_connect_on_real_data(settings, filters):
    items = []
    for name, f in FEEDS.items():
        items += parse_feed(read_fixture_bytes(f), name)

    outcome = Prefilter(filters).run(items)
    for n, it in enumerate(outcome.candidates, 1):
        it.id = f"s{n}"
        it.body = it.title

    class DummyState:
        def quota_remaining(self, n):
            return 999

        def consume_quota(self, n=1):
            pass

    def caller(prompt):
        ids = [ln.split("id: ")[1].split("\n")[0]
               for ln in prompt.split("---\n")[1:]]
        return json.dumps(
            [{"id": i, "relevant": True, "deadline": "2026-09-01",
              "amount_jpy": 2000000, "amount_known": True} for i in ids]
        )

    judge = Judge(settings, caller, DummyState(), sleeper=lambda _: None)
    verdicts, carried = judge.run(outcome.candidates)
    assert carried == []
    assert len(verdicts) == len(outcome.candidates)

    ranked = rank_all(outcome.candidates, verdicts, [], settings, date(2026, 8, 1))
    assert len(ranked) == len(outcome.candidates)
    assert all(r.rank in ("S", "A", "B", "excluded") for r in ranked)


# --- 設定ファイルの健全性 ---


def test_all_sources_have_required_keys():
    for s in config.load_sources():
        assert s.get("name"), s
        assert s.get("top", "").startswith("http"), s
        assert s.get("method") in ("feed", "scrape"), s
        assert s.get("status") in ("ok", "todo", "no_feed"), s


def test_source_count_matches_spec():
    """§1 の 29機関（確定4 + 探索23 + スクレイプ2）。"""
    sources = config.load_sources()
    assert len(sources) == 29
    assert sum(1 for s in sources if s["method"] == "scrape") == 2
    assert sum(1 for s in sources if s["method"] == "feed") == 27


def test_spec_confirmed_feeds_are_ok():
    """§1-1 の4機関は探索不要。常に status: ok で feed が入っている。"""
    by_name = {s["name"]: s for s in config.load_sources()}
    for name in ("沖縄県", "那覇市", "宜野湾市", "沖縄市"):
        assert by_name[name]["status"] == "ok", name
        assert by_name[name]["feed"], name


def test_source_names_are_unique():
    names = [s["name"] for s in config.load_sources()]
    assert len(names) == len(set(names))


def test_settings_match_spec_table():
    """§4-1 の表の値がそのまま入っていること。"""
    g = config.load_settings()["gemini"]
    assert g["batch_size"] == 10
    assert g["min_interval_sec"] == 4.1
    assert g["max_requests_per_run"] == 30
    assert g["max_requests_per_day"] == 60
    assert g["backoff_sec"] == [8, 16, 32]
    assert g["max_retries"] == 3


def test_detail_limit_is_3000_chars():
    assert config.load_settings()["detail"]["max_chars"] == 3000


def test_limit_defers_rest_instead_of_dropping(offline, monkeypatch, tmp_path):
    """--limit で溢れた候補は捨てずに pending へ回す（seen 済みのため）。"""
    state = State(data_dir=tmp_path, dry_run=True)
    monkeypatch.setattr(main_mod, "State", lambda **kw: state)

    def fake_caller_builder(settings, args):
        return lambda prompt: json.dumps([{"id": "s1", "relevant": True}])

    monkeypatch.setattr(main_mod, "_build_caller", fake_caller_builder)

    class Args1(Args):
        limit = 1
        no_ai = False

    main_mod.run(Args1())
    assert len(state.pending) > 1, "溢れた分が pending に積まれていない"
    assert all(p["body"] for p in state.pending)
