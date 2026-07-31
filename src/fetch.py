"""フィード取得と本文抽出（§3・§9）。

Fetcher が HTTP を一手に握り、巡回マナー（UA・ホスト間隔・タイムアウト・
リトライ・robots.txt）をここだけで担保する。他モジュールは requests を
直接呼ばない。
"""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import feedparser
import requests

log = logging.getLogger(__name__)


@dataclass
class FeedItem:
    """フィード1件。判定パイプラインを流れる最小単位。"""

    source: str
    title: str
    url: str
    published: str | None = None
    summary: str = ""
    # 詳細取得後に埋まる
    body: str = ""
    pdf_only: bool = False
    id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "published": self.published,
            "summary": self.summary,
            "body": self.body,
            "pdf_only": self.pdf_only,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FeedItem":
        return cls(
            source=d.get("source", ""),
            title=d.get("title", ""),
            url=d.get("url", ""),
            published=d.get("published"),
            summary=d.get("summary", ""),
            body=d.get("body", ""),
            pdf_only=bool(d.get("pdf_only")),
            id=d.get("id", ""),
        )


@dataclass
class FetchStats:
    ok: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    new_items: int = 0
    detail_failed: int = 0


class Fetcher:
    """巡回マナーを守る HTTP クライアント（§9）。"""

    def __init__(self, settings: dict, user_agent: str):
        http = settings["http"]
        self.timeout = http["timeout_sec"]
        self.retries = http["retries"]
        self.host_interval = http["host_interval_sec"]
        self.respect_robots = http.get("respect_robots", True)
        self.max_bytes = settings["detail"]["max_bytes"]
        self.user_agent = user_agent
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self._last_access: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # --- 内部 ---

    def _wait_for_host(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_access.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.host_interval:
                time.sleep(self.host_interval - elapsed)
        self._last_access[host] = time.monotonic()

    def _robots_for(self, url: str):
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base in self._robots:
            return self._robots[base]
        rp = urllib.robotparser.RobotFileParser()
        try:
            self._wait_for_host(base)
            resp = self.session.get(
                urljoin(base, "/robots.txt"), timeout=self.timeout
            )
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None  # robots.txt が無ければ制限なしとみなす
        except requests.RequestException:
            rp = None
        self._robots[base] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True

    # --- 公開 API ---

    def get(self, url: str, check_robots: bool = True) -> requests.Response | None:
        """1回リトライ付きで GET。失敗時は None。"""
        if check_robots and not self.allowed(url):
            log.warning("robots.txt により取得しない: %s", url)
            return None
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                self._wait_for_host(url)
                resp = self.session.get(url, timeout=self.timeout, stream=True)
                content = resp.raw.read(self.max_bytes + 1, decode_content=True)
                if len(content) > self.max_bytes:
                    log.warning("本文が大きすぎるため打ち切り: %s", url)
                    content = content[: self.max_bytes]
                resp._content = content
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_err = e
                if attempt < self.retries:
                    time.sleep(1.0)
        log.warning("取得失敗 %s: %s", url, last_err)
        return None


def parse_feed(content: bytes, source_name: str, base_url: str = "") -> list[FeedItem]:
    """フィードのバイト列を FeedItem のリストにする（ネット不要・テスト可能）。"""
    parsed = feedparser.parse(content)
    items: list[FeedItem] = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        if base_url:
            link = urljoin(base_url, link)
        title = (entry.get("title") or "").strip()
        published = _entry_date(entry)
        summary = (entry.get("summary") or "").strip()
        items.append(
            FeedItem(
                source=source_name,
                title=title,
                url=link,
                published=published,
                summary=summary[:500],
            )
        )
    return items


def _entry_date(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                pass
    for key in ("published", "updated"):
        v = entry.get(key)
        if v:
            return str(v)[:10]
    return None


def fetch_source(fetcher: Fetcher, source: dict) -> tuple[list[FeedItem], bool]:
    """1機関のフィードを取得。(items, ok) を返す。

    フィードは robots.txt の対象外とする（§9 の注記）。
    """
    feed_url = source.get("feed")
    if not feed_url:
        return [], False
    resp = fetcher.get(feed_url, check_robots=False)
    if resp is None:
        return [], False
    try:
        items = parse_feed(resp.content, source["name"], base_url=source.get("top", ""))
    except Exception as e:
        log.warning("フィード解析失敗 %s: %s", source["name"], e)
        return [], False
    # 200 でも HTML が返る（リニューアル後など）ケースは失敗扱いにして
    # discover.py の再探索対象に載せる
    if not items:
        return [], False
    return items, True


# --- 本文抽出（§3） ---

_PDF_HINT = (".pdf", ".PDF")


def extract_text(html: str, url: str = "") -> str:
    """HTML から本文テキストを抽出。trafilatura → BeautifulSoup の順。"""
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(html, url=url or None) or ""
    except Exception:
        text = ""
    if len(text.strip()) >= 50:
        return _normalize(text)
    return _normalize(_soup_text(html))


def _soup_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    main = soup.find("main") or soup.find(id="main") or soup.find("body") or soup
    return main.get_text("\n", strip=True)


def _normalize(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def fetch_detail(
    fetcher: Fetcher, item: FeedItem, max_chars: int
) -> bool:
    """候補1件の本文を取得して item.body を埋める。成功なら True。

    取得失敗（404/タイムアウト）はスキップし、Gemini は呼ばない（§3）。
    """
    if item.url.endswith(_PDF_HINT):
        # リンク先が PDF そのもの。v1 では開かない（§3）
        item.pdf_only = True
        item.body = f"{item.title}\n（リンク先はPDF。本文未取得）"
        return True
    resp = fetcher.get(item.url)
    if resp is None:
        return False
    ctype = resp.headers.get("Content-Type", "")
    if "pdf" in ctype.lower():
        item.pdf_only = True
        item.body = f"{item.title}\n（リンク先はPDF。本文未取得）"
        return True
    resp.encoding = resp.apparent_encoding or resp.encoding
    text = extract_text(resp.text, item.url)
    # 本文が薄く、PDF リンクだけのページ（§3）
    if len(text) < 120 and ".pdf" in resp.text.lower():
        item.pdf_only = True
    item.body = text[:max_chars]
    return True
