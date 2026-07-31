"""落札結果の抽出と蓄積（§6）。

Gemini は使わない。正規表現で取れた分だけ拾い、取れなくても
URL とタイトルは必ず保存する。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date

from .fetch import FeedItem

# 全角数字・カンマを含む金額
_NUM = r"[0-9０-９][0-9０-９,，]*"

AMOUNT_PATTERNS = [
    re.compile(rf"落札(?:金額|価格)[^0-9０-９]{{0,10}}({_NUM})\s*(?:円|万円)"),
    re.compile(rf"契約(?:金額|額)[^0-9０-９]{{0,10}}({_NUM})\s*(?:円|万円)"),
    re.compile(rf"(?:金額|価格)[^0-9０-９]{{0,6}}({_NUM})\s*(?:円|万円)"),
]
# 「万円」表記かどうかを後段で判定するために同じ位置を再検出する
_MAN_SUFFIX = re.compile(rf"({_NUM})\s*万円")

WINNER_PATTERNS = [
    re.compile(r"落札(?:者|業者|事業者|人)(?:名)?[\s:：]{0,4}([^\s、,。\n]{2,40})"),
    re.compile(r"契約(?:の)?相手方(?:名)?[\s:：]{0,4}([^\s、,。\n]{2,40})"),
    # 「受託候補者」「選定事業者」「特定予定者」等。実ページで頻出する
    re.compile(
        r"(?:受託|選定|特定|決定)(?:予定)?(?:候補)?(?:者|事業者|業者)(?:名)?"
        r"[\s:：]{0,4}([^\s、,。\n]{2,40})"
    ),
]

OPEN_DATE_PATTERNS = [
    re.compile(r"開札(?:日|日時)[\s:：]{0,4}(?:令和(\d+|元)年)?\s*(\d{1,2})月\s*(\d{1,2})日"),
    re.compile(r"開札(?:日|日時)[\s:：]{0,4}(\d{4})[-/年]\s*(\d{1,2})[-/月]\s*(\d{1,2})"),
]

NAME_PATTERNS = [
    re.compile(r"(?:案件|業務|工事|件)名[\s:：]{0,4}([^\n]{4,80})"),
]

_ZEN2HAN = str.maketrans("０１２３４５６７８９，", "0123456789,")


def _to_int(s: str) -> int | None:
    s = s.translate(_ZEN2HAN).replace(",", "")
    return int(s) if s.isdigit() else None


def extract_amount(text: str) -> int | None:
    for pat in AMOUNT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        val = _to_int(m.group(1))
        if val is None:
            continue
        # 「万円」表記なら 1万倍する
        tail = text[m.start() : m.end()]
        if _MAN_SUFFIX.search(tail):
            val *= 10000
        return val
    return None


def extract_winner(text: str) -> str | None:
    for pat in WINNER_PATTERNS:
        m = pat.search(text)
        if m:
            name = m.group(1).strip(" 　:：")
            # 見出しだけ拾ってしまったケースを弾く
            if name and not name.startswith(("名", "は")) and len(name) >= 2:
                return name[:40]
    return None


def extract_open_date(text: str, default_year: int) -> str | None:
    for pat in OPEN_DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        groups = m.groups()
        if len(groups) == 3 and groups[0] and len(str(groups[0])) == 4:
            y, mo, d = int(groups[0]), int(groups[1]), int(groups[2])
        elif len(groups) == 3 and groups[0]:
            era = 1 if groups[0] == "元" else int(groups[0])
            y, mo, d = 2018 + era, int(groups[1]), int(groups[2])
        else:
            y, mo, d = default_year, int(groups[-2]), int(groups[-1])
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def extract_name(text: str, fallback: str) -> str:
    for pat in NAME_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()[:80]
    return fallback


def build_record(item: FeedItem, text: str, today: date, recorded_at: str) -> dict:
    """1件の落札結果レコードを作る。取れなかった項目は None のまま。"""
    body = text or ""
    return {
        "url": item.url,
        "source": item.source,
        "title": item.title,
        "name": extract_name(body, item.title),
        "winner": extract_winner(body),
        "amount_jpy": extract_amount(body),
        "open_date": extract_open_date(body, today.year),
        "published": item.published,
        "recorded_at": recorded_at,
    }


def monthly_summary(records: list[dict], year: int, month: int) -> str:
    """月次サマリ本文（§6）。機関別件数 / 落札者頻度上位10 / 金額判明分。"""
    lines = [f"=== {year}年{month}月 落札結果サマリ ===", f"件数: {len(records)}件", ""]

    by_source = Counter(r.get("source", "不明") for r in records)
    lines.append("--- 機関別件数 ---")
    for name, n in by_source.most_common():
        lines.append(f"  {name}: {n}件")
    lines.append("")

    winners = Counter(r["winner"] for r in records if r.get("winner"))
    lines.append("--- 落札者 頻度上位10 ---")
    if winners:
        for name, n in winners.most_common(10):
            lines.append(f"  {name}: {n}件")
    else:
        lines.append("  （落札者を抽出できた案件なし）")
    lines.append("")

    priced = [r for r in records if r.get("amount_jpy")]
    lines.append(f"--- 金額が取れた案件 ({len(priced)}件) ---")
    for r in sorted(priced, key=lambda x: -x["amount_jpy"]):
        lines.append(f"  {r['amount_jpy']:,}円  {r.get('name') or r['title']}")
        lines.append(f"      {r.get('source')} / {r.get('winner') or '落札者不明'}")
        lines.append(f"      {r['url']}")
    if not priced:
        lines.append("  （金額を抽出できた案件なし）")
    return "\n".join(lines)
