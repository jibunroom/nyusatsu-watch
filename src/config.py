"""設定ファイル（config/*.yml）と .env の読み込み。

パス解決を一箇所に集約する。テストからは ROOT を差し替えず、
load_* に明示パスを渡す形で使う。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_settings(path: Path | None = None) -> dict:
    return _load_yaml(path or CONFIG_DIR / "settings.yml")


def load_filters(path: Path | None = None) -> dict:
    return _load_yaml(path or CONFIG_DIR / "filters.yml")


def load_sources(path: Path | None = None) -> list[dict]:
    data = _load_yaml(path or CONFIG_DIR / "sources.yml")
    return data.get("sources", []) if isinstance(data, dict) else []


def save_sources(sources: list[dict], path: Path | None = None) -> None:
    """discover.py が feed/status を書き戻す。

    コメントは保持できないため、先頭に固定ヘッダを付け直す。
    """
    path = path or CONFIG_DIR / "sources.yml"
    header = (
        "# 監視対象機関（§1）\n"
        "#\n"
        "# status:\n"
        "#   ok      … feed 確定。日次巡回の対象\n"
        "#   todo    … 未探索。次回の discover.py が feed を探す\n"
        "#   no_feed … 探索したが見つからなかった（サマリメールに列挙される）\n"
        "#\n"
        "# method:\n"
        "#   feed    … RSS/Atom を巡回\n"
        "#   scrape  … HTML スクレイピング（v1 未実装・§1-3）\n"
        "#\n"
        "# fail_streak … 日次巡回の連続失敗回数。3 に達すると再探索される\n"
        "#\n"
        "# ※このファイルは discover.py が自動更新するためコメントは保持されない。\n"
        "# 機関を追加するときは name / top / method: feed / status: todo を書けばよい\n"
        "# （離島市町村もこれで足せる）。top URL は実 HTTP で 200 を確認すること。\n\n"
    )
    body = yaml.safe_dump(
        {"sources": sources},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    path.write_text(header + body, encoding="utf-8")


def load_env() -> None:
    """.env があれば読む（GitHub Actions では Secrets が環境変数で入る）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv 未導入でも動かす
        return
    load_dotenv(ROOT / ".env")


def env(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def user_agent(settings: dict) -> str:
    tpl = settings["http"]["user_agent"]
    return tpl.replace("{mail_to}", env("MAIL_TO", "unknown") or "unknown")
