"""§1-4 フィード探索。HTML 解析部分をネット無しでテストする。"""
from src.discover import FAIL_THRESHOLD, candidates_from_html, is_valid_feed, needs_discovery
from tests.conftest import read_fixture, read_fixture_bytes


def test_finds_link_rel_alternate():
    html = """<html><head>
    <link rel="alternate" type="application/rss+xml" href="/news.rss">
    <link rel="stylesheet" href="/a.css">
    </head><body></body></html>"""
    out = candidates_from_html(html, "https://www.city.x.lg.jp/")
    assert out == ["https://www.city.x.lg.jp/news.rss"]


def test_finds_atom_and_rdf_types():
    html = """<html><head>
    <link rel="alternate" type="application/atom+xml" href="/atom.xml">
    <link rel="alternate" type="application/rdf+xml" href="/index.rdf">
    </head></html>"""
    out = candidates_from_html(html, "https://x.lg.jp/")
    assert "https://x.lg.jp/atom.xml" in out
    assert "https://x.lg.jp/index.rdf" in out


def test_finds_anchor_with_hint():
    html = """<html><body>
    <a href="/shinchaku.xml">新着情報</a>
    <a href="/data/report.xml">統計データ</a>
    <a href="/feed/list.rss">RSS配信</a>
    </body></html>"""
    out = candidates_from_html(html, "https://x.lg.jp/")
    assert "https://x.lg.jp/shinchaku.xml" in out
    assert "https://x.lg.jp/feed/list.rss" in out
    # ヒント語が無いものは拾わない
    assert "https://x.lg.jp/data/report.xml" not in out


def test_anchor_must_end_with_feed_extension():
    html = '<html><body><a href="/rss/index.html">RSS について</a></body></html>'
    assert candidates_from_html(html, "https://x.lg.jp/") == []


def test_anchor_with_query_string():
    html = '<html><body><a href="/cgi-bin/feed.php?siteNew=1">新着</a></body></html>'
    # .php?... は拡張子判定を通らない（手順3の直接試行で拾う想定）
    assert candidates_from_html(html, "https://x.lg.jp/") == []


def test_deduplicates_and_keeps_order():
    html = """<html><head>
    <link rel="alternate" type="application/rss+xml" href="/news.rss">
    </head><body><a href="/news.rss">RSS</a></body></html>"""
    assert candidates_from_html(html, "https://x.lg.jp/") == ["https://x.lg.jp/news.rss"]


def test_candidates_on_real_page():
    """実サイトの HTML でも例外なく動くこと。"""
    out = candidates_from_html(
        read_fixture("page_naha_proposal.html"), "https://www.city.naha.okinawa.jp/"
    )
    assert isinstance(out, list)


def test_is_valid_feed_accepts_real_feeds():
    for name in ("feed_naha.xml", "feed_ginowan.rdf", "feed_okinawa_city.xml"):
        assert is_valid_feed(read_fixture_bytes(name)), name


def test_is_valid_feed_rejects_html():
    assert not is_valid_feed(b"<html><body>404 Not Found</body></html>")


def test_is_valid_feed_rejects_empty_feed():
    xml = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    assert not is_valid_feed(xml)


# --- 再探索の判定 ---


def test_needs_discovery_for_todo():
    assert needs_discovery({"name": "x", "status": "todo", "method": "feed"})


def test_needs_discovery_for_no_feed():
    assert needs_discovery({"name": "x", "status": "no_feed", "method": "feed"})


def test_ok_source_is_skipped():
    src = {"name": "x", "status": "ok", "feed": "https://x/f.rss", "method": "feed"}
    assert not needs_discovery(src)


def test_ok_source_rediscovered_after_consecutive_failures():
    """3回連続失敗ならサイトリニューアルを疑って再探索（§1-4）。"""
    src = {"name": "x", "status": "ok", "feed": "https://x/f.rss",
           "method": "feed", "fail_streak": FAIL_THRESHOLD}
    assert needs_discovery(src)
    src["fail_streak"] = FAIL_THRESHOLD - 1
    assert not needs_discovery(src)


def test_scrape_sources_are_never_discovered():
    """§1-3 スクレイピング枠は v1 では触らない。"""
    assert not needs_discovery(
        {"name": "沖縄防衛局", "status": "todo", "method": "scrape"}
    )


def test_common_paths_include_spec_four():
    """§1-4 手順3 に挙がっている4パスは必ず試す。"""
    from src.discover import COMMON_PATHS
    for p in ("/news.rss", "/rss/10/list1.xml", "/shinchaku.xml",
              "/cgi-bin/feed.php?siteNew=1"):
        assert p in COMMON_PATHS
