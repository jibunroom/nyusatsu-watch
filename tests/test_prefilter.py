"""§2 一次フィルタ。ここで9割落ちること・取りこぼさないことを確かめる。"""
import pytest

from src.fetch import parse_feed
from src.prefilter import DROP, PASS, RESULT, Prefilter
from tests.conftest import read_fixture_bytes


@pytest.fixture
def pf(filters):
    return Prefilter(filters)


@pytest.mark.parametrize(
    "title",
    [
        "ホームページ制作業務委託の公募について",
        "業務システム開発に係る一般競争入札の実施",
        "広報動画制作業務のプロポーザル",
        "データ入力業務の見積合わせ",
        "文書電子化業務の随意契約について",
        "LINE公式アカウント運用支援業務 企画提案公募",
    ],
)
def test_pass_it_and_creative(pf, title):
    assert pf.classify(title) == PASS


@pytest.mark.parametrize(
    "title",
    [
        "市道1号線舗装工事の一般競争入札",
        "庁舎清掃業務委託の入札公告",
        "学校給食調理業務委託の公募",
        "公園除草業務の見積",
        "庁舎警備業務委託 一般競争入札",
        "浄化槽維持管理業務委託の公告",
        "公用車両購入の一般競争入札",
        "インフルエンザワクチン接種業務委託",
        "橋梁修繕工事に係る入札公告",
    ],
)
def test_drop_construction_and_services(pf, title):
    assert pf.classify(title) == DROP


def test_drop_when_no_include_keyword(pf):
    assert pf.classify("市民課の窓口時間変更のお知らせ") == DROP
    assert pf.classify("令和8年度 予算の概要") == DROP


def test_include_matches_url_too(pf):
    """タイトルに語が無くても URL に含まれれば候補（§2-1）。"""
    assert pf.classify("お知らせ", "https://www.city.x.lg.jp/nyusatsu/2026.html") == DROP
    assert pf.classify("お知らせ", "https://www.city.x.lg.jp/入札/2026.html") == PASS


# --- §2-2 注記: 「設計」の共起ルール ---


def test_design_alone_is_not_excluded(pf):
    assert pf.classify("システム設計業務委託の公募") == PASS
    assert pf.classify("Webデザイン業務委託の見積") == PASS


def test_design_with_civil_engineering_is_excluded(pf):
    assert pf.classify("市民会館 実施設計業務委託（建築）の公告") == DROP
    assert pf.classify("道路詳細設計業務の一般競争入札") == DROP


def test_design_with_it_context_is_rescued(pf):
    """建築土木語と共起しても IT 語があれば除外せず Gemini に回す。"""
    assert pf.classify("庁舎情報システム基本設計業務委託の公募") == PASS


# --- §2-3 落札結果の分岐 ---


@pytest.mark.parametrize(
    "title",
    [
        "令和8年度 一般競争入札結果について",
        "公共施設整備工事の落札結果",
        "開札結果の公表",
        "契約結果の公表について",
    ],
)
def test_result_branch(pf, title):
    assert pf.classify(title) == RESULT


def test_result_branch_wins_over_exclude(pf):
    """工事の落札結果も §6 の蓄積対象として拾う。"""
    assert pf.classify("道路舗装工事の落札結果について") == RESULT


# --- 実フィードでの挙動 ---


def test_real_feeds_drop_most(pf):
    """29機関分の実データで、一次フィルタが大半を落とすこと（§2 前文）。"""
    items = []
    for f, name in [
        ("feed_okinawa_pref.xml", "沖縄県"),
        ("feed_naha.xml", "那覇市"),
        ("feed_ginowan.rdf", "宜野湾市"),
        ("feed_okinawa_city.xml", "沖縄市"),
    ]:
        items += parse_feed(read_fixture_bytes(f), name)

    out = pf.run(items)
    assert out.total == len(items)
    # 候補は全体の半分未満であるべき（Gemini に渡す量を絞れている）
    assert len(out.candidates) < len(items) / 2, (
        f"絞り込みが弱い: {len(out.candidates)}/{len(items)}"
    )


def test_real_feed_keeps_known_it_case(pf):
    """実データ中の本命案件を取りこぼさないこと。"""
    items = parse_feed(read_fixture_bytes("feed_okinawa_pref.xml"), "沖縄県")
    titles = [i.title for i in items if pf.classify(i.title, i.url) == PASS]
    assert any("税務システム" in t for t in titles), titles
