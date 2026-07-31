"""一次フィルタ（§2）。AI 不使用・ここで9割落とす。

原則: 判断に迷うものは除外せず Gemini に回す（取りこぼし防止優先）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .fetch import FeedItem

# 判定結果の分類
PASS = "pass"        # 候補（詳細取得 → Gemini へ）
RESULT = "result"    # 落札結果（§6 の別処理へ）
DROP = "drop"        # 除外


@dataclass
class PrefilterOutcome:
    candidates: list[FeedItem] = field(default_factory=list)
    results: list[FeedItem] = field(default_factory=list)
    dropped: int = 0

    @property
    def total(self) -> int:
        return len(self.candidates) + len(self.results) + self.dropped


class Prefilter:
    def __init__(self, filters: dict):
        self.include = filters.get("include", [])
        self.exclude = filters.get("exclude", [])
        self.result_markers = filters.get("result_markers", [])
        self.cooccurrence = filters.get("exclude_cooccurrence", [])
        self.rescue = filters.get("exclude_rescue", [])

    def classify(self, title: str, url: str = "") -> str:
        """1件を PASS / RESULT / DROP のいずれかに分類する。"""
        haystack = f"{title} {url}"

        # §2-3 落札結果は判定パイプラインに乗せない。除外語より先に見る
        # （「工事の入札結果」も結果としては記録したいため）
        if any(kw in title for kw in self.result_markers):
            return RESULT

        # §2-1 まず通す条件（タイトルまたはURL）
        if not any(kw in haystack for kw in self.include):
            return DROP

        # §2-2 即除外（タイトルのみで判定）
        if any(kw in title for kw in self.exclude):
            return DROP

        # 「設計」の共起ルール。IT 文脈の語があれば救済して Gemini に回す
        if self._excluded_by_cooccurrence(title):
            return DROP

        return PASS

    def _excluded_by_cooccurrence(self, title: str) -> bool:
        for rule in self.cooccurrence:
            left = rule.get("left", [])
            right = rule.get("right", [])
            if any(w in title for w in left) and any(w in title for w in right):
                if any(w in title for w in self.rescue):
                    return False  # 迷うものは除外せず Gemini へ
                return True
        return False

    def run(self, items: list[FeedItem]) -> PrefilterOutcome:
        out = PrefilterOutcome()
        for item in items:
            verdict = self.classify(item.title, item.url)
            if verdict == PASS:
                out.candidates.append(item)
            elif verdict == RESULT:
                out.results.append(item)
            else:
                out.dropped += 1
        return out
