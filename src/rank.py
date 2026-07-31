"""ランク付け（§5-1）。

§0 のパイプラインで [rank] は独立した工程なので、判定（judge）とも
通知（notify）とも分けて単独モジュールにしてある。

S / A / B / 除外 の 4 分類:
  S     … すぐ動ける本命
  A     … 壁はあるが人間が見る価値あり（judge_failed もここ）
  B     … relevant=false。記録のみ・通知しない
  除外  … 金額1000万超 / 重い実績要件の大型プロポーザル
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from .fetch import FeedItem

S = "S"
A = "A"
B = "B"
EXCLUDED = "excluded"

# 「実績◯件以上」「◯年以上の実績」など、小規模法人には重い要件（§5-1 除外）
HEAVY_REQUIREMENT = re.compile(
    r"(実績[^\n]{0,10}?\d+\s*件以上"
    r"|過去[^\n]{0,10}?\d+\s*年[^\n]{0,6}?実績"
    r"|同種[^\n]{0,20}?\d+\s*件以上"
    r"|\d+\s*年以上[^\n]{0,10}?実績)"
)


@dataclass
class Ranked:
    item: FeedItem
    rank: str
    verdict: dict
    days_left: int | None = None
    warn_staff: bool = False
    judge_failed: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_record(self) -> dict:
        d = self.item.to_dict()
        # 本文は items.json に残すと肥大化するので落とす
        d.pop("body", None)
        d.update(
            {
                "rank": self.rank,
                "days_left": self.days_left,
                "warn_staff": self.warn_staff,
                "judge_failed": self.judge_failed,
                "verdict": self.verdict,
            }
        )
        return d


def parse_deadline(value) -> date | None:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def days_until(deadline: date | None, today: date) -> int | None:
    return None if deadline is None else (deadline - today).days


def _has_barrier(v: dict) -> list[str]:
    """A 行きの壁を列挙する（§5-1 の A 行）。

    S 行の「(資格不要 or 随意契約)」と A 行の「入札参加資格の壁」は
    そのままだと重なる。随意契約には入札参加資格審査が無いので、
    随意契約のときだけ資格の壁を打ち消す、という読みで両立させる。
    他の壁（実績・地域・Pマーク）は随意契約でも残る。
    """
    barriers = []
    if v.get("qualification_needed") and v.get("method") != "随意契約":
        barriers.append("入札参加資格")
    if v.get("experience_required"):
        barriers.append("実績要件")
    if v.get("region_limit") in ("市内", "県内"):
        barriers.append(f"地域要件({v['region_limit']})")
    if v.get("security_cert_required"):
        barriers.append("Pマーク等")
    return barriers


def rank_one(
    item: FeedItem,
    verdict: dict | None,
    settings: dict,
    today: date,
) -> Ranked:
    cfg = settings["rank"]
    limit = cfg["amount_limit_jpy"]
    safe_days = cfg["deadline_safe_days"]
    staff_warn = cfg["staff_warn_threshold"]

    # judge_failed: AI 判定なしでも情報は落とさず A 枠に載せる（§4-3）
    if verdict is None:
        return Ranked(item=item, rank=A, verdict={}, judge_failed=True,
                      reasons=["AI判定失敗"])

    amount = verdict.get("amount_jpy")
    amount_known = bool(verdict.get("amount_known")) and isinstance(
        amount, (int, float)
    )
    deadline = parse_deadline(verdict.get("deadline"))
    days_left = days_until(deadline, today)
    warn_staff = _int(verdict.get("staff_required")) >= staff_warn

    r = Ranked(item=item, rank=B, verdict=verdict, days_left=days_left,
               warn_staff=warn_staff)

    # 除外: 金額判明かつ上限超
    if amount_known and amount > limit:
        r.rank = EXCLUDED
        r.reasons.append(f"予定価格 {int(amount):,}円 > {limit:,}円")
        return r

    # 除外: 重い実績要件の大型プロポーザル
    heavy = HEAVY_REQUIREMENT.search(item.body or "")
    if heavy:
        r.rank = EXCLUDED
        r.reasons.append(f"重い実績要件: {heavy.group(0)[:20]}")
        return r

    if not verdict.get("relevant"):
        r.rank = B
        return r

    barriers = _has_barrier(verdict)
    tight = days_left is not None and days_left < safe_days
    unknown_deadline = days_left is None

    # S: 資格不要 or 随意契約 / 締切まで7日以上 / 金額不明または上限以下
    qualification_free = (
        not verdict.get("qualification_needed")
        or verdict.get("method") == "随意契約"
    )
    if qualification_free and not tight and not unknown_deadline and not barriers:
        r.rank = S
        return r

    # それ以外の relevant はすべて A
    r.rank = A
    if barriers:
        r.reasons.extend(barriers)
    if tight:
        r.reasons.append(f"締切まで{days_left}日")
    if unknown_deadline:
        r.reasons.append("締切不明")
    return r


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def rank_all(
    items: list[FeedItem],
    verdicts: dict[str, dict],
    failed: list[FeedItem],
    settings: dict,
    today: date,
) -> list[Ranked]:
    failed_ids = {i.id for i in failed}
    out = []
    for item in items:
        v = None if item.id in failed_ids else verdicts.get(item.id)
        out.append(rank_one(item, v, settings, today))
    return out


def sort_for_mail(ranked: list[Ranked]) -> list[Ranked]:
    """S → A、同ランク内は締切が近い順（不明は最後）。"""
    order = {S: 0, A: 1, B: 2, EXCLUDED: 3}
    return sorted(
        ranked,
        key=lambda r: (
            order.get(r.rank, 9),
            r.days_left if r.days_left is not None else 10**6,
        ),
    )
