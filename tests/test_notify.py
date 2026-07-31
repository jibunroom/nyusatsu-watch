"""§5-2 メールの件名・本文。SMTP には接続しない。"""
from datetime import date

from src import notify
from src.fetch import FeedItem
from src.notify import RunSummary, build_body, build_subject, format_item
from src.rank import rank_one

TODAY = date(2026, 8, 1)


def make(title, rank_hint=None, **over):
    v = {
        "id": "s1", "relevant": True, "category": "it",
        "amount_jpy": 3500000, "amount_known": True,
        "deadline": "2026-08-20", "deadline_known": True,
        "method": "プロポーザル", "qualification_needed": False,
        "region_limit": "なし", "experience_required": False,
        "security_cert_required": False, "staff_required": 0,
        "automation_potential": "high", "reason": "勤怠データ集計業務。コード化で工数1/10",
    }
    v.update(over)
    item = FeedItem(source="沖縄県", title=title, url="https://x.jp/1", id="s1")
    return item, v


def rank(title, settings, **over):
    item, v = make(title, **over)
    return rank_one(item, v, settings, TODAY)


def summary(**over) -> RunSummary:
    s = RunSummary(
        sources_ok=27, sources_total=29, failed_sources=["恩納村", "東村"],
        new_items=214, prefilter_passed=18, gemini_requests=2,
        counts={"S": 1, "A": 3, "B": 14}, results_recorded=5,
        quota_used=4, quota_limit=60,
    )
    for k, val in over.items():
        setattr(s, k, val)
    return s


def test_subject_with_s(settings):
    ranked = [rank("勤怠管理システム構築業務", settings),
              rank("広報動画制作", settings, deadline="2026-08-03")]
    subj = build_subject(ranked, summary())
    assert subj.startswith("[入札S] 勤怠管理システム構築業務")
    assert "他1件" in subj
    assert "締切 2026-08-20" in subj


def test_subject_single_s_has_no_others(settings):
    subj = build_subject([rank("単独案件", settings)], summary())
    assert "他" not in subj


def test_subject_a_only(settings):
    ranked = [rank("案件1", settings, qualification_needed=True),
              rank("案件2", settings, region_limit="市内")]
    assert build_subject(ranked, summary()) == "[入札A] 2件"


def test_subject_zero(settings):
    """0件でも生存通知（§5-2）。"""
    subj = build_subject([], summary())
    assert subj == "[入札] 本日0件・巡回27/29機関"


def test_subject_warn_marker(settings):
    ranked = [rank("大型案件", settings, staff_required=6)]
    assert "⚠️" in build_subject(ranked, summary())


def test_format_item_matches_spec_layout(settings):
    r = rank("勤怠データ集計業務", settings)
    text = format_item(r)
    assert text.splitlines()[0] == "■ [S] 勤怠データ集計業務"
    assert "機関: 沖縄県 / 方式: プロポーザル / 締切: 2026-08-20 (残19日)" in text
    assert "金額: 350万円 / 資格: 不要 / 自動化余地: high" in text
    assert "理由: 勤怠データ集計業務。コード化で工数1/10" in text
    assert "URL: https://x.jp/1" in text


def test_format_item_judge_failed(settings):
    item = FeedItem(source="那覇市", title="判定失敗案件", url="https://x.jp/9", id="s9")
    r = rank_one(item, None, settings, TODAY)
    text = format_item(r)
    assert "[A]" in text
    assert "AI判定失敗" in text
    assert "URL: https://x.jp/9" in text


def test_format_item_pdf_only(settings):
    r = rank("PDF案件", settings)
    r.item.pdf_only = True
    assert "[PDFのみ]" in format_item(r)


def test_body_includes_summary_block(settings):
    body = build_body([rank("案件", settings)], summary())
    assert "--- 実行サマリ ---" in body
    assert "巡回: 27/29機関成功 (失敗: 恩納村, 東村)" in body
    assert "新着: 214件 / 一次通過: 18件 / Gemini判定: 2リクエスト" in body
    assert "S:1 A:3 B:14 / 落札結果: 5件記録" in body
    assert "Gemini本日使用: 4/60リクエスト" in body


def test_body_excludes_b_rank(settings):
    b = rank("対象外案件", settings, relevant=False)
    s = rank("本命案件", settings)
    body = build_body([b, s], summary())
    assert "本命案件" in body
    assert "対象外案件" not in body


def test_body_zero_items(settings):
    body = build_body([], summary())
    assert "該当案件はありませんでした。" in body
    assert "--- 実行サマリ ---" in body


def test_summary_optional_lines():
    s = summary(detail_failed=3, judge_failed_batches=1, pending_carried=12,
                no_feed=["東村", "国頭村"])
    out = s.render()
    assert "本文取得失敗: 3件" in out
    assert "判定失敗バッチ: 1件" in out
    assert "次回持ち越し: 12件" in out
    assert "フィード未発見: 2件 (東村, 国頭村)" in out


def test_send_mail_dry_run_does_not_connect(capsys):
    ok = notify.send_mail("件名", "本文", {}, dry_run=True)
    assert ok is True
    assert "件名" in capsys.readouterr().out


def test_send_mail_missing_config_fails_gracefully():
    assert notify.send_mail("件名", "本文", {"host": "", "port": 465}) is False


def test_subject_picks_nearest_deadline_s(settings):
    """件名の最重要案件は締切が最も近い S（並び順に依存しない）。"""
    ranked = [rank("遠いS", settings, deadline="2026-09-30"),
              rank("近いS", settings, deadline="2026-08-15")]
    assert build_subject(ranked, summary()).startswith("[入札S] 近いS")
