"""§6 落札結果の抽出。Gemini は使わない。"""
from datetime import date

import pytest

from src.fetch import FeedItem, extract_text
from src.results import (
    build_record,
    extract_amount,
    extract_open_date,
    extract_winner,
    monthly_summary,
)
from tests.conftest import read_fixture

TODAY = date(2026, 8, 1)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("落札金額 3,500,000円", 3500000),
        ("落札価格：1,234,567 円", 1234567),
        ("契約金額 850万円", 8500000),
        ("落札金額　１，０００，０００円", 1000000),
        ("金額の記載なし", None),
    ],
)
def test_extract_amount(text, expected):
    assert extract_amount(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("落札者 株式会社 okinawa商事", "株式会社"),
        ("落札者名：有限会社テスト", "有限会社テスト"),
        ("契約の相手方 合同会社アルファ", "合同会社アルファ"),
        ("受託候補者\n丸紅株式会社", "丸紅株式会社"),
        ("該当なし", None),
    ],
)
def test_extract_winner(text, expected):
    assert extract_winner(text) == expected


def test_extract_open_date_reiwa():
    assert extract_open_date("開札日 令和8年7月28日", 2026) == "2026-07-28"


def test_extract_open_date_western():
    assert extract_open_date("開札日：2026-07-28", 2026) == "2026-07-28"


def test_extract_open_date_month_day_only_uses_default_year():
    assert extract_open_date("開札日 7月28日", 2026) == "2026-07-28"


def test_extract_open_date_invalid_returns_none():
    assert extract_open_date("開札日 令和8年13月45日", 2026) is None


def test_real_result_page(fixtures):
    """実ページ（宜野湾市 公用車管理システム 選定結果）から抽出できること。"""
    text = extract_text(read_fixture("page_ginowan_result.html"))
    item = FeedItem(
        source="宜野湾市",
        title="宜野湾市公用車管理システム導入業務に係る公募型プロポーザル選定結果について",
        url="https://www.city.ginowan.lg.jp/x/20723.html",
    )
    rec = build_record(item, text, TODAY, "2026-08-01T07:30:00+09:00")
    assert rec["winner"] == "丸紅株式会社"
    assert rec["source"] == "宜野湾市"
    assert rec["url"].startswith("https://")


def test_record_kept_even_when_nothing_extracted():
    """金額・業者名が取れなくても URL とタイトルは保存する（§6）。"""
    item = FeedItem(source="東村", title="入札結果の公表", url="https://x.jp/1")
    rec = build_record(item, "", TODAY, "2026-08-01T07:30:00+09:00")
    assert rec["url"] == "https://x.jp/1"
    assert rec["title"] == "入札結果の公表"
    assert rec["name"] == "入札結果の公表"
    assert rec["winner"] is None
    assert rec["amount_jpy"] is None


def test_monthly_summary():
    records = [
        {"source": "那覇市", "title": "A", "name": "A業務", "winner": "甲社",
         "amount_jpy": 5000000, "url": "https://x.jp/a", "recorded_at": "2026-07-02"},
        {"source": "那覇市", "title": "B", "name": "B業務", "winner": "甲社",
         "amount_jpy": None, "url": "https://x.jp/b", "recorded_at": "2026-07-05"},
        {"source": "沖縄県", "title": "C", "name": "C業務", "winner": None,
         "amount_jpy": 12000000, "url": "https://x.jp/c", "recorded_at": "2026-07-09"},
    ]
    out = monthly_summary(records, 2026, 7)
    assert "件数: 3件" in out
    assert "那覇市: 2件" in out
    assert "甲社: 2件" in out
    # 金額は降順
    assert out.index("12,000,000円") < out.index("5,000,000円")


def test_monthly_summary_empty():
    out = monthly_summary([], 2026, 7)
    assert "件数: 0件" in out
    assert "落札者を抽出できた案件なし" in out
