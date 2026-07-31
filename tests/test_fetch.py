"""§11-1 ネット無しで fetch のパース部分をテストする。"""
import pytest

from src.fetch import extract_text, parse_feed
from tests.conftest import read_fixture, read_fixture_bytes

FEEDS = [
    ("feed_okinawa_pref.xml", "沖縄県"),
    ("feed_naha.xml", "那覇市"),
    ("feed_ginowan.rdf", "宜野湾市"),
    ("feed_okinawa_city.xml", "沖縄市"),
]


@pytest.mark.parametrize("filename,source", FEEDS)
def test_parse_real_feeds(filename, source):
    """4機関の実フィード（RSS 2.0 / RDF 両方）が読めること。"""
    items = parse_feed(read_fixture_bytes(filename), source)
    assert items, f"{filename} から1件も取れていない"
    for item in items:
        assert item.source == source
        assert item.title.strip()
        assert item.url.startswith("http")


def test_parse_feed_extracts_dates():
    items = parse_feed(read_fixture_bytes("feed_naha.xml"), "那覇市")
    dated = [i for i in items if i.published]
    assert dated, "published が1件も取れていない"
    assert all(len(i.published) >= 10 for i in dated)


def test_parse_feed_skips_entries_without_link():
    xml = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
    <title>t</title>
    <item><title>リンク無し</title></item>
    <item><title>あり</title><link>https://example.jp/a.html</link></item>
    </channel></rss>""".encode()
    items = parse_feed(xml, "テスト市")
    assert len(items) == 1
    assert items[0].title == "あり"


def test_parse_feed_resolves_relative_links():
    xml = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
    <title>t</title>
    <item><title>相対</title><link>/news/1.html</link></item>
    </channel></rss>""".encode()
    items = parse_feed(xml, "テスト市", base_url="https://www.example.lg.jp/")
    assert items[0].url == "https://www.example.lg.jp/news/1.html"


def test_parse_feed_on_garbage_returns_empty():
    assert parse_feed(b"<html><body>not a feed</body></html>", "x") == []


@pytest.mark.parametrize(
    "filename,must_contain",
    [
        ("page_naha_proposal.html", "公募型プロポーザル"),
        ("page_pref_system.html", "税務システム"),
        ("page_ginowan_result.html", "選定結果"),
    ],
)
def test_extract_text_from_real_pages(filename, must_contain):
    text = extract_text(read_fixture(filename))
    assert len(text) > 200, "本文が短すぎる（抽出に失敗している）"
    assert must_contain in text
    # ナビ・スクリプトが残っていないこと
    assert "<script" not in text
    assert "function(" not in text


def test_extract_text_falls_back_to_soup():
    """trafilatura が拾えない短い HTML でも BeautifulSoup で取れること。"""
    html = "<html><body><script>var a=1;</script><p>入札公告 テスト</p></body></html>"
    text = extract_text(html)
    assert "入札公告 テスト" in text
    assert "var a=1" not in text
