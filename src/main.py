"""エントリポイント（§0 のパイプライン統括）。

fetch → dedupe → prefilter → detail → judge → rank → notify → persist

例外で落ちた場合も可能な限り `[入札] 実行エラー` メールを送る（§8-1）。
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta

from . import config, notify, rank as rank_mod, results as results_mod
from .fetch import FeedItem, Fetcher, fetch_detail, fetch_source
from .judge import Judge, make_gemini_caller
from .notify import RunSummary
from .prefilter import Prefilter
from .state import JST, State, now_jst_iso

log = logging.getLogger(__name__)


def _smtp_cfg(settings: dict) -> dict:
    return {
        "host": config.env("SMTP_HOST", settings["mail"]["smtp_host"]),
        "port": config.env("SMTP_PORT", str(settings["mail"]["smtp_port"])),
        "user": config.env("SMTP_USER"),
        "password": config.env("SMTP_PASS"),
        "mail_to": config.env("MAIL_TO"),
    }


def collect(fetcher, sources, state, summary) -> list[FeedItem]:
    """全機関のフィードを巡回し、未取得の新着だけを返す（fetch + dedupe）。"""
    fresh: list[FeedItem] = []
    for src in sources:
        if src.get("method") == "scrape":
            continue  # §1-3 v1 では未実装
        # フィード未確定の機関も分母に数える。「4/4成功」と出して
        # 23機関が未接続であることを隠さないため
        summary.sources_total += 1
        if src.get("status") != "ok" or not src.get("feed"):
            summary.no_feed.append(src["name"])
            continue
        items, ok = fetch_source(fetcher, src)
        if not ok:
            summary.failed_sources.append(src["name"])
            src["fail_streak"] = int(src.get("fail_streak", 0)) + 1
            continue
        summary.sources_ok += 1
        src["fail_streak"] = 0
        for item in items:
            if state.is_seen(src["name"], item.url):
                continue
            state.mark_seen(src["name"], item.url)
            fresh.append(item)
    summary.new_items = len(fresh)
    return fresh


def handle_results(fetcher, items, state, settings, today) -> int:
    """§6 落札結果。Gemini を使わず正規表現で抽出して蓄積する。"""
    max_chars = settings["detail"]["max_chars"]
    recorded = 0
    for item in items:
        if not fetch_detail(fetcher, item, max_chars):
            # 本文が取れなくても URL とタイトルだけは残す（§6）
            item.body = ""
        rec = results_mod.build_record(item, item.body, today, now_jst_iso())
        if state.add_result(rec):
            recorded += 1
    return recorded


def maybe_monthly_summary(state, smtp, today, dry_run: bool) -> None:
    """毎月1日の朝実行で先月分を集計してメール（§6）。"""
    if today.day != 1:
        return
    if datetime.now(JST).hour >= 12:
        return  # 朝実行のみ
    last = today - timedelta(days=1)
    records = state.results_in_month(last.year, last.month)
    body = results_mod.monthly_summary(records, last.year, last.month)
    subject = f"[入札] {last.year}年{last.month}月 落札結果サマリ（{len(records)}件）"
    notify.send_mail(subject, body + "\n", smtp, dry_run=dry_run)


def git_persist(dry_run: bool) -> None:
    """data/ と config/ をコミット & push（§7）。変更が無ければ何もしない。"""
    if dry_run:
        log.info("dry-run のため git commit/push をスキップ")
        return
    try:
        subprocess.run(["git", "add", "data/", "config/"],
                       cwd=config.ROOT, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"],
                              cwd=config.ROOT)
        if diff.returncode == 0:
            log.info("変更なし。コミットしない")
            return
        msg = f"chore: 状態更新 {now_jst_iso()}"
        subprocess.run(["git", "commit", "-m", msg], cwd=config.ROOT, check=True)
        subprocess.run(["git", "push"], cwd=config.ROOT, check=True)
        log.info("コミット & push 完了")
    except subprocess.CalledProcessError as e:
        log.error("git 操作に失敗: %s", e)


def run(args) -> int:
    config.load_env()
    settings = config.load_settings()
    filters = config.load_filters()
    sources = config.load_sources()
    smtp = _smtp_cfg(settings)
    today = datetime.now(JST).date()

    state = State(dry_run=args.dry_run)
    summary = RunSummary(quota_limit=settings["gemini"]["max_requests_per_day"])
    fetcher = Fetcher(settings, config.user_agent(settings))

    # --- fetch + dedupe ---
    # collect() は失敗した機関の fail_streak を増やす。値が動いたときだけ
    # sources.yml を書き戻す（毎回書くとコメントが消えるため）
    fail_before = [s.get("fail_streak", 0) for s in sources]
    fresh = collect(fetcher, sources, state, summary)
    fail_changed = [s.get("fail_streak", 0) for s in sources] != fail_before
    log.info("新着 %d件", len(fresh))

    # --- prefilter（§2） ---
    outcome = Prefilter(filters).run(fresh)
    summary.prefilter_passed = len(outcome.candidates)
    log.info(
        "一次通過 %d件 / 結果系 %d件 / 除外 %d件",
        len(outcome.candidates), len(outcome.results), outcome.dropped,
    )

    # --- 落札結果（§6・Gemini を使わない） ---
    summary.results_recorded = handle_results(
        fetcher, outcome.results, state, settings, today
    )

    # --- 前回の持ち越しを先に消化（§4-1） ---
    pending = [FeedItem.from_dict(d) for d in state.pending]
    candidates = pending + outcome.candidates
    if pending:
        log.info("持ち越し %d件を先に処理", len(pending))

    # --- detail（§3）。持ち越し分は本文取得済み ---
    max_chars = settings["detail"]["max_chars"]
    ready: list[FeedItem] = []
    for item in candidates:
        if item.body:
            ready.append(item)
            continue
        if fetch_detail(fetcher, item, max_chars):
            ready.append(item)
        else:
            # 取得失敗は Gemini を呼ばずスキップ（§3）
            summary.detail_failed += 1

    # --limit で溢れた分は捨てずに次回へ回す。
    # seen 済みなので捨てると二度と拾えなくなる
    deferred: list[FeedItem] = []
    if args.limit is not None and len(ready) > args.limit:
        log.info("--limit %d のため %d件を次回に回す", args.limit,
                 len(ready) - args.limit)
        deferred = ready[args.limit :]
        ready = ready[: args.limit]

    for n, item in enumerate(ready, 1):
        item.id = f"s{n}"

    # --- judge（§4） ---
    verdicts: dict[str, dict] = {}
    carried: list[FeedItem] = []
    judge_stats = None
    if ready:
        caller = _build_caller(settings, args)
        if caller is None:
            # キーが無い/no-ai。判定なしで A 枠に流し、情報は落とさない
            carried = []
            judge_failed = list(ready)
        else:
            judge = Judge(settings, caller, state)
            verdicts, carried = judge.run(ready)
            judge_stats = judge.stats
            judge_failed = judge.stats.failed_items
            summary.gemini_requests = judge.stats.requests
            summary.judge_failed_batches = judge.stats.failed_batches
    else:
        judge_failed = []

    # 同値な FeedItem を取り違えないよう同一性で比較する
    carried_ids = {id(i) for i in carried}
    judged_items = [i for i in ready if id(i) not in carried_ids]
    state.set_pending([i.to_dict() for i in carried + deferred])
    summary.pending_carried = len(carried) + len(deferred)
    if judge_stats and judge_stats.quota_stopped:
        log.warning("Gemini 上限到達。%d件を次回に持ち越す", len(carried))

    # --- rank（§5-1） ---
    ranked = rank_mod.rank_all(judged_items, verdicts, judge_failed, settings, today)
    for r in ranked:
        summary.counts[r.rank] = summary.counts.get(r.rank, 0) + 1
    state.add_items([r.to_record() for r in ranked])

    # --- notify（§5-2）。0件でも必ず送る ---
    summary.quota_used = state.quota_count
    subject = notify.build_subject(ranked, summary)
    body = notify.build_body(ranked, summary)
    notify.send_mail(subject, body, smtp, dry_run=args.dry_run)

    maybe_monthly_summary(state, smtp, today, args.dry_run)

    # --- persist（§7） ---
    state.save(seen_limit=settings["state"]["seen_max_per_source"])
    if not args.dry_run and fail_changed:
        config.save_sources(sources)
    git_persist(args.dry_run)
    return 0


def _build_caller(settings: dict, args):
    if args.no_ai:
        log.info("--no-ai のため Gemini を呼ばない")
        return None
    api_key = config.env("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY が無いため判定をスキップ")
        return None
    model = config.env("GEMINI_MODEL", settings["gemini"]["default_model"])
    return make_gemini_caller(api_key, model, settings["gemini"]["timeout_sec"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="nyusatsu-watch（§0）")
    parser.add_argument("--dry-run", action="store_true",
                        help="メール送信と git push をスキップし標準出力に表示")
    parser.add_argument("--limit", type=int, default=None,
                        help="Gemini に渡す件数の上限（--limit 1 で1件実測）")
    parser.add_argument("--no-ai", action="store_true",
                        help="Gemini を一切呼ばない（全件A枠として出力）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run(args)
    except Exception:
        tb = traceback.format_exc()
        log.error("実行エラー:\n%s", tb)
        try:
            config.load_env()
            settings = config.load_settings()
            notify.send_mail(
                "[入札] 実行エラー",
                f"巡回中に例外が発生しました。\n\n{tb}\n",
                _smtp_cfg(settings),
                dry_run=args.dry_run,
            )
        except Exception:
            log.error("エラーメールの送信にも失敗")
        return 1


if __name__ == "__main__":
    sys.exit(main())
