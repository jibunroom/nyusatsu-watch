"""メール生成・送信（§5-2）。

本文はプレーンテキスト。0件でも必ず送る（生存通知）。
送信は smtplib + email（PHPMailer 相当）。
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formatdate

from . import rank as rank_mod
from .rank import Ranked

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    """メール末尾の実行サマリ（§5-2）に出す数値。"""

    sources_ok: int = 0
    sources_total: int = 0
    failed_sources: list[str] = field(default_factory=list)
    new_items: int = 0
    prefilter_passed: int = 0
    gemini_requests: int = 0
    counts: dict = field(default_factory=lambda: {"S": 0, "A": 0, "B": 0})
    results_recorded: int = 0
    quota_used: int = 0
    quota_limit: int = 0
    detail_failed: int = 0
    judge_failed_batches: int = 0
    pending_carried: int = 0
    no_feed: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = ["--- 実行サマリ ---"]
        failed = (
            f" (失敗: {', '.join(self.failed_sources)})" if self.failed_sources else ""
        )
        lines.append(f"巡回: {self.sources_ok}/{self.sources_total}機関成功{failed}")
        lines.append(
            f"新着: {self.new_items}件 / 一次通過: {self.prefilter_passed}件 / "
            f"Gemini判定: {self.gemini_requests}リクエスト"
        )
        lines.append(
            f"S:{self.counts.get('S', 0)} A:{self.counts.get('A', 0)} "
            f"B:{self.counts.get('B', 0)} / 落札結果: {self.results_recorded}件記録"
        )
        lines.append(
            f"Gemini本日使用: {self.quota_used}/{self.quota_limit}リクエスト"
        )
        if self.detail_failed:
            lines.append(f"本文取得失敗: {self.detail_failed}件")
        if self.judge_failed_batches:
            lines.append(f"判定失敗バッチ: {self.judge_failed_batches}件（A枠に掲載）")
        if self.pending_carried:
            lines.append(f"次回持ち越し: {self.pending_carried}件")
        if self.no_feed:
            lines.append(
                f"フィード未発見: {len(self.no_feed)}件 ({', '.join(self.no_feed)})"
            )
        return "\n".join(lines)


def _fmt_amount(v) -> str:
    if not isinstance(v, (int, float)):
        return "不明"
    man = int(v) // 10000
    return f"{man}万円" if man else f"{int(v):,}円"


def _fmt_deadline(r: Ranked) -> str:
    dl = r.verdict.get("deadline")
    if not dl or not r.verdict.get("deadline_known", True):
        return "不明"
    if r.days_left is None:
        return str(dl)
    return f"{dl} (残{r.days_left}日)"


def format_item(r: Ranked) -> str:
    """1件分の本文ブロック（§5-2 の書式）。"""
    v = r.verdict
    warn = " ⚠️" if r.warn_staff else ""
    pdf = " [PDFのみ]" if r.item.pdf_only else ""
    lines = [f"■ [{r.rank}]{warn} {r.item.title}{pdf}"]

    if r.judge_failed:
        # AI 判定なし。人間が見れば分かるよう情報だけ並べる（§4-3）
        lines.append(f"  機関: {r.item.source} / ※AI判定失敗のため未分類")
    else:
        lines.append(
            f"  機関: {r.item.source} / 方式: {v.get('method', '不明')} / "
            f"締切: {_fmt_deadline(r)}"
        )
        qual = "必要" if v.get("qualification_needed") else "不要"
        lines.append(
            f"  金額: {_fmt_amount(v.get('amount_jpy'))} / 資格: {qual} / "
            f"自動化余地: {v.get('automation_potential', '不明')}"
        )
        if v.get("reason"):
            lines.append(f"  理由: {v['reason']}")
    if r.reasons:
        lines.append(f"  注意: {' / '.join(r.reasons)}")
    lines.append(f"  URL: {r.item.url}")
    return "\n".join(lines)


def build_subject(ranked: list[Ranked], summary: RunSummary) -> str:
    # 件名に出す「最重要案件」は締切が最も近い S にする
    ordered = rank_mod.sort_for_mail(ranked)
    s_items = [r for r in ordered if r.rank == rank_mod.S]
    a_items = [r for r in ordered if r.rank == rank_mod.A]
    warn = "⚠️" if any(r.warn_staff for r in s_items + a_items) else ""
    if s_items:
        top = s_items[0]
        others = len(s_items) + len(a_items) - 1
        dl = top.verdict.get("deadline") or "不明"
        tail = f" 他{others}件" if others > 0 else ""
        return f"[入札S]{warn} {top.item.title}{tail}（締切 {dl}）"
    if a_items:
        return f"[入札A]{warn} {len(a_items)}件"
    return (
        f"[入札] 本日0件・巡回{summary.sources_ok}/{summary.sources_total}機関"
    )


def build_body(ranked: list[Ranked], summary: RunSummary) -> str:
    notable = [r for r in ranked if r.rank in (rank_mod.S, rank_mod.A)]
    notable = rank_mod.sort_for_mail(notable)
    parts = []
    if notable:
        parts.append("\n\n".join(format_item(r) for r in notable))
    else:
        parts.append("該当案件はありませんでした。")
    parts.append(summary.render())
    return "\n\n".join(parts) + "\n"


def send_mail(subject: str, body: str, cfg: dict, dry_run: bool = False) -> bool:
    """SMTP 送信。dry_run なら標準出力に出すだけ（§11-3）。

    cfg: host / port / user / password / mail_to
    """
    if dry_run:
        print("=" * 60)
        print(f"Subject: {subject}")
        print("=" * 60)
        print(body)
        return True

    missing = [k for k in ("host", "user", "password", "mail_to") if not cfg.get(k)]
    if missing:
        log.error("SMTP 設定が不足: %s", ", ".join(missing))
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg["user"]
    msg["To"] = cfg["mail_to"]
    msg["Date"] = formatdate(localtime=True)

    try:
        with smtplib.SMTP_SSL(cfg["host"], int(cfg["port"]), timeout=30) as smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.sendmail(cfg["user"], [cfg["mail_to"]], msg.as_string())
        log.info("メール送信完了: %s", subject)
        return True
    except (smtplib.SMTPException, OSError) as e:
        log.error("メール送信失敗: %s", e)
        return False
