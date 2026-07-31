"""§5-1 ランク付け。"""
from datetime import date

import pytest

from src import rank as rank_mod
from src.fetch import FeedItem
from src.rank import A, B, EXCLUDED, S, rank_one

TODAY = date(2026, 8, 1)


def item(title="案件", body="") -> FeedItem:
    return FeedItem(source="沖縄県", title=title, url="https://x.jp/1",
                    body=body, id="s1")


def verdict(**over) -> dict:
    v = {
        "id": "s1",
        "relevant": True,
        "category": "it",
        "amount_jpy": 3500000,
        "amount_known": True,
        "deadline": "2026-08-20",
        "deadline_known": True,
        "method": "プロポーザル",
        "qualification_needed": False,
        "region_limit": "なし",
        "experience_required": False,
        "security_cert_required": False,
        "staff_required": 0,
        "automation_potential": "high",
        "reason": "テスト",
    }
    v.update(over)
    return v


def test_s_rank(settings):
    r = rank_one(item(), verdict(), settings, TODAY)
    assert r.rank == S
    assert r.days_left == 19


def test_a_when_qualification_needed(settings):
    r = rank_one(item(), verdict(qualification_needed=True), settings, TODAY)
    assert r.rank == A
    assert "入札参加資格" in r.reasons


def test_s_when_qualification_needed_but_zuii(settings):
    """資格が要っても随意契約なら S（§5-1 の S 行）。"""
    r = rank_one(
        item(),
        verdict(qualification_needed=True, method="随意契約"),
        settings,
        TODAY,
    )
    assert r.rank == S


def test_zuii_does_not_waive_other_barriers(settings):
    """随意契約でも実績・地域要件は壁として残る（§5-1 の A 行）。"""
    r = rank_one(
        item(),
        verdict(qualification_needed=True, method="随意契約", region_limit="市内"),
        settings,
        TODAY,
    )
    assert r.rank == A
    assert "地域要件(市内)" in r.reasons


def test_a_when_deadline_tight(settings):
    r = rank_one(item(), verdict(deadline="2026-08-05"), settings, TODAY)
    assert r.rank == A
    assert "締切まで4日" in r.reasons


def test_a_when_deadline_unknown(settings):
    r = rank_one(
        item(), verdict(deadline=None, deadline_known=False), settings, TODAY
    )
    assert r.rank == A
    assert "締切不明" in r.reasons


@pytest.mark.parametrize(
    "over,expected_reason",
    [
        ({"experience_required": True}, "実績要件"),
        ({"region_limit": "市内"}, "地域要件(市内)"),
        ({"security_cert_required": True}, "Pマーク等"),
    ],
)
def test_a_when_barrier(settings, over, expected_reason):
    r = rank_one(item(), verdict(**over), settings, TODAY)
    assert r.rank == A
    assert expected_reason in r.reasons


def test_b_when_not_relevant(settings):
    r = rank_one(item(), verdict(relevant=False), settings, TODAY)
    assert r.rank == B


def test_excluded_over_amount_limit(settings):
    r = rank_one(item(), verdict(amount_jpy=15000000), settings, TODAY)
    assert r.rank == EXCLUDED


def test_not_excluded_when_amount_unknown(settings):
    """金額不明は除外しない（§5-1 の S 条件）。"""
    r = rank_one(
        item(), verdict(amount_jpy=None, amount_known=False), settings, TODAY
    )
    assert r.rank == S


def test_excluded_on_heavy_experience_requirement(settings):
    body = "参加資格: 過去5年間に同種業務の実績3件以上を有すること。"
    r = rank_one(item(body=body), verdict(), settings, TODAY)
    assert r.rank == EXCLUDED
    assert "重い実績要件" in r.reasons[0]


def test_staff_warn_not_excluded(settings):
    """staff_required >= 5 は除外せず ⚠️ を付ける（§5-1）。"""
    r = rank_one(item(), verdict(staff_required=6), settings, TODAY)
    assert r.rank == S
    assert r.warn_staff is True


def test_judge_failed_goes_to_a(settings):
    """AI 判定失敗でも情報を落とさず A 枠（§4-3）。"""
    r = rank_one(item(), None, settings, TODAY)
    assert r.rank == A
    assert r.judge_failed is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-08-20", date(2026, 8, 20)),
        ("2026/08/20", date(2026, 8, 20)),
        ("2026年8月20日", date(2026, 8, 20)),
        ("未定", None),
        (None, None),
    ],
)
def test_parse_deadline(raw, expected):
    assert rank_mod.parse_deadline(raw) == expected


def test_sort_for_mail_puts_s_first_then_nearest_deadline(settings):
    a_far = rank_one(item("A遠"), verdict(deadline="2026-08-03"), settings, TODAY)
    s_one = rank_one(item("S"), verdict(), settings, TODAY)
    a_near = rank_one(item("A近"), verdict(deadline="2026-08-02"), settings, TODAY)
    out = rank_mod.sort_for_mail([a_far, s_one, a_near])
    assert [r.item.title for r in out] == ["S", "A近", "A遠"]


def test_to_record_drops_body(settings):
    r = rank_one(item(body="長い本文" * 500), verdict(), settings, TODAY)
    rec = r.to_record()
    assert "body" not in rec
    assert rec["rank"] == S
    assert rec["verdict"]["category"] == "it"
