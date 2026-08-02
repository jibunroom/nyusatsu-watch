"""data/*.json の読み書きと Gemini クォータ管理（§4-1・§7）。

すべての状態はリポジトリにコミットされる前提。書き込みは原子的に行う
（一時ファイル → rename）。実行が途中で落ちても JSON が壊れないようにする。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config

JST = timezone(timedelta(hours=9))


def today_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def now_jst_iso() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # 壊れていたら初期値に戻す。落とすより継続を優先する。
        return default


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class State:
    """data/ 配下の状態をまとめて扱う。

    dry_run=True のときは save() が何もしない（§11-3）。
    """

    def __init__(self, data_dir: Path | None = None, dry_run: bool = False):
        self.dir = Path(data_dir or config.DATA_DIR)
        self.dry_run = dry_run
        self.seen: dict[str, list[str]] = _read_json(self.dir / "seen.json", {})
        self.pending: list[dict] = _read_json(self.dir / "pending.json", [])
        self.results: list[dict] = _read_json(self.dir / "results.json", [])
        self.quota: dict = _read_json(
            self.dir / "quota.json", {"date": today_jst(), "count": 0}
        )
        self.items: list[dict] = _read_json(self.dir / "items.json", [])
        # メール送信に失敗した S/A。次回の通知に載せ直す（送信成功まで消さない）
        self.undelivered: list[dict] = _read_json(self.dir / "undelivered.json", [])
        self._seen_index = {k: set(v) for k, v in self.seen.items()}
        self._result_urls = {r.get("url") for r in self.results}
        self._roll_quota()

    # --- seen（取得済みURL・機関ごと） ---

    def is_seen(self, source: str, url: str) -> bool:
        return url in self._seen_index.get(source, ())

    def mark_seen(self, source: str, url: str) -> None:
        self._seen_index.setdefault(source, set()).add(url)
        lst = self.seen.setdefault(source, [])
        lst.append(url)

    def _trim_seen(self, limit: int) -> None:
        """機関ごと最大 limit 件でローテーション（古いものから捨てる）。"""
        for source, urls in self.seen.items():
            if len(urls) > limit:
                self.seen[source] = urls[-limit:]
                self._seen_index[source] = set(self.seen[source])

    # --- quota（当日の Gemini リクエスト数） ---

    def _roll_quota(self) -> None:
        if self.quota.get("date") != today_jst():
            self.quota = {"date": today_jst(), "count": 0}

    @property
    def quota_count(self) -> int:
        return int(self.quota.get("count", 0))

    def quota_remaining(self, daily_limit: int) -> int:
        return max(0, daily_limit - self.quota_count)

    def consume_quota(self, n: int = 1) -> None:
        self._roll_quota()
        self.quota["count"] = self.quota_count + n

    # --- pending（未判定の持ち越し） ---

    def set_pending(self, items: list[dict]) -> None:
        self.pending = items

    # --- results（落札結果） ---

    def add_result(self, rec: dict) -> bool:
        """URL で重複排除しつつ追記。新規なら True。"""
        url = rec.get("url")
        if url in self._result_urls:
            return False
        self._result_urls.add(url)
        self.results.append(rec)
        return True

    def results_in_month(self, year: int, month: int) -> list[dict]:
        prefix = f"{year:04d}-{month:02d}"
        return [
            r for r in self.results if str(r.get("recorded_at", "")).startswith(prefix)
        ]

    # --- items（判定済み全記録） ---

    def add_items(self, items: list[dict]) -> None:
        self.items.extend(items)

    # --- undelivered（送信できなかった通知） ---

    def set_undelivered(self, records: list[dict]) -> None:
        self.undelivered = records

    # --- 永続化 ---

    def save(self, seen_limit: int = 10000) -> None:
        if self.dry_run:
            return
        self._trim_seen(seen_limit)
        _write_json(self.dir / "seen.json", self.seen)
        _write_json(self.dir / "pending.json", self.pending)
        _write_json(self.dir / "results.json", self.results)
        _write_json(self.dir / "quota.json", self.quota)
        _write_json(self.dir / "items.json", self.items)
        _write_json(self.dir / "undelivered.json", self.undelivered)
