"""Gemini 判定（§4）。バッチ・検証・バックオフ・クォータ厳守。

Gemini の呼び出しは `caller` 関数に切り出してある。テストではここを
差し替えることで、API を叩かずに検証ロジックだけを試せる（§11-2）。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from .fetch import FeedItem

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """あなたは公共調達案件の分類器。以下の会社が受注可能かを判定する。
会社: 沖縄県宜野湾市の小規模法人。得意分野は
(a)データ入力・集計・電子化・OCR等の事務のシステム化
(b)Web制作・システム開発・LINE公式・DX支援
(c)写真・動画撮影・ドローン・広報物制作
(d)事務局運営・IT研修
除外: 建設工事系・物品購入・清掃警備等の役務、予定価格1000万円超。
各案件についてJSONのみ返す。説明文・マークダウン禁止。

出力は次の形の JSON 配列のみ。入力の id をすべて過不足なく含めること。
[{"id":"s1","relevant":true,"category":"data|it|creative|office|other",
"amount_jpy":3500000,"amount_known":true,"deadline":"2026-08-20",
"deadline_known":true,"method":"一般競争|プロポーザル|指名|随意契約|不明",
"qualification_needed":true,"region_limit":"市内|県内|なし|不明",
"experience_required":true,"security_cert_required":false,
"staff_required":0,"automation_potential":"high|mid|low","reason":"40字以内"}]"""

RETRY_NOTE = "前回の出力はJSONとして不正だった。JSON配列のみを返せ。"

# 欠けていたらスキーマ不一致とみなすキー。
# 残りのキーは既定値で補う（1キーの欠落で再送してトークンを捨てないため）。
REQUIRED_KEYS = ("id", "relevant")

DEFAULTS = {
    "category": "other",
    "amount_jpy": None,
    "amount_known": False,
    "deadline": None,
    "deadline_known": False,
    "method": "不明",
    "qualification_needed": False,
    "region_limit": "不明",
    "experience_required": False,
    "security_cert_required": False,
    "staff_required": 0,
    "automation_potential": "low",
    "reason": "",
}


class QuotaExceeded(Exception):
    """当日/当実行のリクエスト上限に達した。未判定分は持ち越す。"""


@dataclass
class JudgeStats:
    requests: int = 0
    judged: int = 0
    failed_batches: int = 0
    failed_items: list[FeedItem] = field(default_factory=list)
    quota_stopped: bool = False


def build_prompt(batch: list[FeedItem], retry: bool = False) -> str:
    """1バッチ分のユーザープロンプトを組み立てる（§4-2）。"""
    lines = []
    for item in batch:
        lines.append(
            f"---\nid: {item.id}\n機関名: {item.source}\n"
            f"タイトル: {item.title}\n本文抜粋:\n{item.body}"
        )
    prompt = "以下の案件を判定せよ。\n\n" + "\n".join(lines)
    if retry:
        prompt += f"\n\n{RETRY_NOTE}"
    return prompt


def extract_json_array(text: str) -> list:
    """最初の `[` 〜 最後の `]` を抽出して json.loads（§4-3）。"""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSON配列が見つからない")
    return json.loads(text[start : end + 1])


def validate(parsed, expected_ids: list[str]) -> list[dict]:
    """スキーマと id の過不足を検証し、既定値で正規化する（§4-3）。"""
    if not isinstance(parsed, list):
        raise ValueError("配列ではない")
    out = []
    seen_ids = []
    for row in parsed:
        if not isinstance(row, dict):
            raise ValueError("要素がオブジェクトではない")
        for key in REQUIRED_KEYS:
            if key not in row:
                raise ValueError(f"必須キー欠落: {key}")
        rec = dict(DEFAULTS)
        rec.update(row)
        rec["id"] = str(rec["id"])
        rec["relevant"] = _as_bool(rec["relevant"])
        seen_ids.append(rec["id"])
        out.append(rec)
    if sorted(seen_ids) != sorted(expected_ids):
        raise ValueError(
            f"idの過不足: 期待{sorted(expected_ids)} / 実際{sorted(seen_ids)}"
        )
    return out


def _as_bool(v) -> bool:
    """モデルが "false" / "いいえ" を文字列で返しても正しく解釈する。"""
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "no", "0", "", "いいえ")
    return bool(v)


def make_gemini_caller(api_key: str, model: str, timeout_sec: int = 120):
    """google-genai を使う既定の caller を返す。"""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    def call(prompt: str) -> str:
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        return resp.text or ""

    return call


class Judge:
    def __init__(self, settings: dict, caller, state, sleeper=time.sleep):
        g = settings["gemini"]
        self.batch_size = g["batch_size"]
        self.min_interval = g["min_interval_sec"]
        self.max_per_run = g["max_requests_per_run"]
        self.max_per_day = g["max_requests_per_day"]
        self.backoff = g["backoff_sec"]
        self.max_retries = g["max_retries"]
        self.caller = caller
        self.state = state
        self.sleep = sleeper
        self._last_call: float | None = None
        self.stats = JudgeStats()

    # --- レート制御 ---

    def _throttle(self) -> None:
        if self._last_call is None:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            self.sleep(self.min_interval - elapsed)

    def _budget_left(self) -> int:
        per_run = self.max_per_run - self.stats.requests
        per_day = self.state.quota_remaining(self.max_per_day)
        return min(per_run, per_day)

    def _call_once(self, prompt: str) -> str:
        """1リクエスト。429/503 は指数バックオフで最大3回（§4-1）。"""
        if self._budget_left() <= 0:
            raise QuotaExceeded()
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            self._last_call = time.monotonic()
            self.stats.requests += 1
            self.state.consume_quota(1)
            try:
                return self.caller(prompt)
            except Exception as e:
                last_err = e
                if not _is_retryable(e):
                    raise
                if attempt < self.max_retries - 1:
                    wait = self.backoff[min(attempt, len(self.backoff) - 1)]
                    log.warning("Gemini リトライ %ds: %s", wait, e)
                    self.sleep(wait)
                if self._budget_left() <= 0:
                    raise QuotaExceeded() from last_err
        raise RuntimeError(f"Gemini 3回失敗: {last_err}")

    # --- バッチ判定 ---

    def judge_batch(self, batch: list[FeedItem]) -> list[dict] | None:
        """1バッチを判定。検証に2回失敗したら None（judge_failed）。"""
        expected = [i.id for i in batch]
        for retry in (False, True):
            try:
                raw = self._call_once(build_prompt(batch, retry=retry))
            except QuotaExceeded:
                raise
            except Exception as e:
                log.warning("バッチ呼び出し失敗: %s", e)
                return None
            try:
                return validate(extract_json_array(raw), expected)
            except (ValueError, json.JSONDecodeError) as e:
                log.warning("出力検証失敗(retry=%s): %s", retry, e)
        return None

    def run(self, items: list[FeedItem]) -> tuple[dict[str, dict], list[FeedItem]]:
        """候補全件を判定。(id -> 判定, 未判定で持ち越す件) を返す。"""
        verdicts: dict[str, dict] = {}
        for i in range(0, len(items), self.batch_size):
            batch = items[i : i + self.batch_size]
            if self._budget_left() <= 0:
                # 上限到達。Gemini を呼ばずに残りを持ち越す（§4-1）
                self.stats.quota_stopped = True
                return verdicts, items[i:]
            try:
                result = self.judge_batch(batch)
            except QuotaExceeded:
                self.stats.quota_stopped = True
                return verdicts, items[i:]
            if result is None:
                # 3回失敗/検証失敗 → スキップして継続。A枠に載せる（§4-3）
                self.stats.failed_batches += 1
                self.stats.failed_items.extend(batch)
                continue
            for rec in result:
                verdicts[rec["id"]] = rec
            self.stats.judged += len(result)
        return verdicts, []


def _is_retryable(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}"
    return any(code in text for code in ("429", "503", "RESOURCE_EXHAUSTED",
                                         "UNAVAILABLE", "Timeout", "timeout"))
