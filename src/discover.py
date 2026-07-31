"""フィード自動探索（§1-4）。週1回、discover.yml から実行する。

探索順:
  1. <link rel="alternate" type="application/rss+xml|atom+xml">
  2. アンカーの href が .rss/.rdf/.xml で終わり、テキストかURLに
     rss / feed / 新着 を含むもの
  3. よくあるパスの直接試行（200 かつ XML としてパース可能なら採用）
"""
from __future__ import annotations

import argparse
import logging
import sys
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup

from . import config
from .fetch import Fetcher

log = logging.getLogger(__name__)

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml")
ANCHOR_HINTS = ("rss", "feed", "新着")
COMMON_PATHS = (
    # §1-4 手順3 に挙がっている4つ
    "/news.rss",
    "/rss/10/list1.xml",
    "/shinchaku.xml",
    "/cgi-bin/feed.php?siteNew=1",
    # 沖縄の自治体サイトで実際に使われていた追加パス。
    # 嘉手納町は /atom.xml、八重瀬町は /docs/index.rss でしか配信していない
    "/atom.xml",
    "/rss.xml",
    "/index.rdf",
    "/index.rss",
    "/docs/index.rss",
    "/articles/index.rss",
    "/category/news/index.rss",
    "/rss_news.xml",
    "/shinchaku/shinchaku.xml",
    "/rss/10/list3.xml",
)
# 日次実行でこの回数連続して取得に失敗したら、status: ok でも再探索する
FAIL_THRESHOLD = 3


def candidates_from_html(html: str, base: str) -> list[str]:
    """HTML から手順1・2でフィード候補URLを列挙する（ネット不要）。"""
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []

    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        ltype = (link.get("type") or "").lower()
        href = link.get("href")
        if href and "alternate" in rel and ltype in FEED_TYPES:
            found.append(urljoin(base, href))

    for a in soup.find_all("a", href=True):
        href = a["href"]
        path = href.split("?")[0].lower()
        if not path.endswith((".rss", ".rdf", ".xml")):
            continue
        text = (a.get_text() or "").lower()
        hay = f"{text} {href.lower()}"
        if any(h in hay for h in ANCHOR_HINTS):
            found.append(urljoin(base, href))

    # 重複を除きつつ順序を保つ
    seen, out = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def is_valid_feed(content: bytes) -> bool:
    """XML としてパースでき、エントリが1件以上あるか。"""
    try:
        parsed = feedparser.parse(content)
    except Exception:
        return False
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        return False
    return len(parsed.entries) > 0


def discover_one(fetcher: Fetcher, source: dict) -> str | None:
    """1機関のフィードURLを探す。見つからなければ None。"""
    top = source.get("top")
    if not top:
        return None

    resp = fetcher.get(top)
    candidates: list[str] = []
    if resp is not None:
        resp.encoding = resp.apparent_encoding or resp.encoding
        try:
            candidates = candidates_from_html(resp.text, top)
        except Exception as e:
            log.warning("%s: HTML 解析失敗 %s", source["name"], e)

    # 手順3: よくあるパスの直接試行
    candidates += [urljoin(top, p) for p in COMMON_PATHS]

    for url in candidates:
        r = fetcher.get(url, check_robots=False)
        if r is None or r.status_code != 200:
            continue
        if is_valid_feed(r.content):
            return url
    return None


def needs_discovery(source: dict) -> bool:
    if source.get("method") == "scrape":
        return False  # §1-3 v1 では対象外
    if source.get("status") != "ok" or not source.get("feed"):
        return True
    # サイトリニューアル追従（§1-4）
    return int(source.get("fail_streak", 0)) >= FAIL_THRESHOLD


def run(sources_path=None, settings_path=None, dry_run: bool = False) -> dict:
    config.load_env()
    settings = config.load_settings(settings_path)
    sources = config.load_sources(sources_path)
    fetcher = Fetcher(settings, config.user_agent(settings))

    stats = {"checked": 0, "found": 0, "no_feed": [], "unchanged": 0}
    for src in sources:
        if not needs_discovery(src):
            stats["unchanged"] += 1
            continue
        stats["checked"] += 1
        log.info("探索中: %s (%s)", src["name"], src.get("top"))
        feed = discover_one(fetcher, src)
        if feed:
            src["feed"] = feed
            src["status"] = "ok"
            src["fail_streak"] = 0
            stats["found"] += 1
            log.info("  → 発見: %s", feed)
        else:
            src["status"] = "no_feed"
            stats["no_feed"].append(src["name"])
            log.info("  → 未発見")

    if not dry_run:
        config.save_sources(sources, sources_path)
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="フィード自動探索（§1-4）")
    parser.add_argument("--dry-run", action="store_true",
                        help="sources.yml を書き換えない")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    stats = run(dry_run=args.dry_run)
    print(
        f"探索: {stats['checked']}機関 / 発見: {stats['found']} / "
        f"未発見: {len(stats['no_feed'])} / 対象外: {stats['unchanged']}"
    )
    if stats["no_feed"]:
        print("フィード未発見: " + ", ".join(stats["no_feed"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
