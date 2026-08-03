"""実行スロット判定（GitHub schedule の遅延・欠落対策）。

GitHub には20分おきに起動を試みさせ、実際にパイプラインを走らせるかは
このモジュールが決める。1日2スロット:

  morning … JST 07:37 〜 17:36 の間に1回
  evening … JST 17:37 〜 23:59 の間に1回

既に走ったスロットは data/last_batch.json に記録され、二重実行しない。
深夜（00:00〜07:36）は何もしない。スロットを丸ごと逃しても、案件は
seen.json の未取得分として次のスロットで全部拾われるため取りこぼしはない。

※ workflow の gate ステップから `python3 -m src.slots` として呼ばれる。
   pip install 前に動く必要があるため、このファイルは標準ライブラリのみ使う。
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
MORNING_START = time(7, 37)
EVENING_START = time(17, 37)

LAST_BATCH_PATH = Path(__file__).resolve().parent.parent / "data" / "last_batch.json"


def current_slot(now: datetime) -> str | None:
    """今この瞬間が属するスロット名。深夜帯は None。"""
    t = now.timetz()
    if t >= EVENING_START.replace(tzinfo=now.tzinfo):
        return "evening"
    if t >= MORNING_START.replace(tzinfo=now.tzinfo):
        return "morning"
    return None


def load_last_batch(path: Path | None = None) -> dict:
    p = path or LAST_BATCH_PATH
    try:
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def due_slot(now: datetime, last_batch: dict) -> str | None:
    """走らせるべきスロット。走らせないなら None。"""
    slot = current_slot(now)
    if slot is None:
        return None
    if last_batch.get(slot) == now.strftime("%Y-%m-%d"):
        return None  # このスロットは今日すでに実行済み
    return slot


def main() -> None:
    now = datetime.now(JST)
    slot = due_slot(now, load_last_batch())
    print(f"due={slot or 'none'}")


if __name__ == "__main__":
    main()
