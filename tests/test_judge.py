"""§11-2 Gemini 呼び出しを差し替え、検証ロジックだけをテストする。

このファイルは一度も実 API を叩かない。
"""
import json

import pytest

from src.fetch import FeedItem
from src.judge import (
    Judge,
    QuotaExceeded,
    build_prompt,
    extract_json_array,
    validate,
)
from src.state import State


def make_items(n: int) -> list[FeedItem]:
    return [
        FeedItem(
            source="沖縄県",
            title=f"案件{i}",
            url=f"https://example.jp/{i}",
            body="本文",
            id=f"s{i}",
        )
        for i in range(1, n + 1)
    ]


def verdict_json(ids) -> str:
    return json.dumps(
        [
            {
                "id": i,
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
            for i in ids
        ],
        ensure_ascii=False,
    )


@pytest.fixture
def state(tmp_path):
    return State(data_dir=tmp_path, dry_run=True)


def build_judge(settings, caller, state, **overrides):
    s = json.loads(json.dumps(settings))
    s["gemini"].update(overrides)
    s["gemini"]["min_interval_sec"] = 0  # テストでは待たない
    return Judge(s, caller, state, sleeper=lambda _: None)


# --- §4-3 抽出と検証 ---


def test_extract_json_array_ignores_surrounding_prose():
    raw = "```json\n[{\"id\":\"s1\",\"relevant\":true}]\n```\n以上です。"
    assert extract_json_array(raw) == [{"id": "s1", "relevant": True}]


def test_extract_json_array_without_brackets_raises():
    with pytest.raises(ValueError):
        extract_json_array("JSONを返せませんでした")


def test_validate_fills_defaults():
    out = validate([{"id": "s1", "relevant": True}], ["s1"])
    assert out[0]["method"] == "不明"
    assert out[0]["automation_potential"] == "low"
    assert out[0]["amount_known"] is False


def test_validate_rejects_missing_required_key():
    with pytest.raises(ValueError, match="必須キー欠落"):
        validate([{"id": "s1"}], ["s1"])


def test_validate_rejects_id_mismatch():
    with pytest.raises(ValueError, match="idの過不足"):
        validate([{"id": "s1", "relevant": True}], ["s1", "s2"])


# --- §11-2 の3ケース ---


def test_case1_valid_json(settings, state):
    items = make_items(3)
    calls = []

    def caller(prompt):
        calls.append(prompt)
        return verdict_json(["s1", "s2", "s3"])

    judge = build_judge(settings, caller, state)
    verdicts, carried = judge.run(items)

    assert len(calls) == 1, "3件は1リクエストにまとまるべき（§12）"
    assert set(verdicts) == {"s1", "s2", "s3"}
    assert carried == []
    assert judge.stats.failed_batches == 0


def test_case2_broken_json_retries_once_then_fails(settings, state):
    items = make_items(2)
    calls = []

    def caller(prompt):
        calls.append(prompt)
        return "すみません、JSONを生成できませんでした。"

    judge = build_judge(settings, caller, state)
    verdicts, carried = judge.run(items)

    assert len(calls) == 2, "同一バッチを1回だけ再送する（§4-3）"
    assert "前回の出力はJSONとして不正だった" in calls[1]
    assert verdicts == {}
    assert judge.stats.failed_batches == 1
    assert judge.stats.failed_items == items


def test_case2b_broken_then_valid_on_resend(settings, state):
    items = make_items(2)
    responses = ["こわれています", verdict_json(["s1", "s2"])]

    def caller(prompt):
        return responses.pop(0)

    judge = build_judge(settings, caller, state)
    verdicts, _ = judge.run(items)
    assert set(verdicts) == {"s1", "s2"}
    assert judge.stats.failed_batches == 0


def test_case3_missing_id_is_schema_violation(settings, state):
    items = make_items(3)
    calls = []

    def caller(prompt):
        calls.append(prompt)
        return verdict_json(["s1", "s2"])  # s3 が無い

    judge = build_judge(settings, caller, state)
    verdicts, _ = judge.run(items)

    assert len(calls) == 2
    assert verdicts == {}
    assert judge.stats.failed_batches == 1


# --- §4-1 バッチ・上限・バックオフ ---


def test_batches_at_most_10(settings, state):
    items = make_items(25)
    calls = []

    def caller(prompt):
        n = prompt.count("id: s")
        calls.append(n)
        ids = [ln.split("id: ")[1].split("\n")[0]
               for ln in prompt.split("---\n")[1:]]
        return verdict_json(ids)

    judge = build_judge(settings, caller, state)
    verdicts, carried = judge.run(items)

    assert calls == [10, 10, 5]
    assert len(verdicts) == 25
    assert carried == []


def test_per_run_limit_carries_over(settings, state):
    items = make_items(50)  # 5バッチ必要

    def caller(prompt):
        ids = [ln.split("id: ")[1].split("\n")[0]
               for ln in prompt.split("---\n")[1:]]
        return verdict_json(ids)

    judge = build_judge(settings, caller, state, max_requests_per_run=2)
    verdicts, carried = judge.run(items)

    assert judge.stats.requests == 2
    assert len(verdicts) == 20
    assert len(carried) == 30, "未判定分は次回に持ち越す（§4-1）"
    assert judge.stats.quota_stopped is True


def test_daily_quota_is_shared_across_runs(settings, state):
    """朝夕合算。quota.json に積まれた分だけ当日の残りが減る。"""
    state.consume_quota(59)
    items = make_items(30)

    def caller(prompt):
        ids = [ln.split("id: ")[1].split("\n")[0]
               for ln in prompt.split("---\n")[1:]]
        return verdict_json(ids)

    judge = build_judge(settings, caller, state)
    verdicts, carried = judge.run(items)

    assert judge.stats.requests == 1, "残り1リクエストしか使えないはず"
    assert state.quota_count == 60
    assert len(carried) == 20


def test_no_call_when_daily_quota_exhausted(settings, state):
    state.consume_quota(60)
    items = make_items(5)
    called = []

    judge = build_judge(settings, lambda p: called.append(p) or "[]", state)
    verdicts, carried = judge.run(items)

    assert called == [], "上限到達時は Gemini を呼ばない（§4-1）"
    assert carried == items
    assert verdicts == {}


def test_backoff_on_429_then_succeeds(settings, state):
    items = make_items(1)
    attempts = []
    slept = []

    def caller(prompt):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return verdict_json(["s1"])

    s = json.loads(json.dumps(settings))
    s["gemini"]["min_interval_sec"] = 0
    judge = Judge(s, caller, state, sleeper=slept.append)
    verdicts, _ = judge.run(items)

    assert len(attempts) == 3
    assert slept == [8, 16], "指数バックオフ 8→16 秒（§4-1）"
    assert set(verdicts) == {"s1"}


def test_gives_up_after_3_attempts(settings, state):
    items = make_items(1)
    attempts = []

    def caller(prompt):
        attempts.append(1)
        raise RuntimeError("503 UNAVAILABLE")

    judge = build_judge(settings, caller, state)
    verdicts, carried = judge.run(items)

    # 3回失敗 → そのバッチはスキップして継続（全停止させない）
    assert len(attempts) == 3
    assert judge.stats.failed_batches == 1
    assert carried == []


def test_non_retryable_error_does_not_retry(settings, state):
    items = make_items(1)
    attempts = []

    def caller(prompt):
        attempts.append(1)
        raise ValueError("400 INVALID_ARGUMENT")

    judge = build_judge(settings, caller, state)
    judge.run(items)
    assert len(attempts) == 1


def test_rate_limit_interval_is_enforced(settings, state):
    items = make_items(20)
    slept = []

    def caller(prompt):
        ids = [ln.split("id: ")[1].split("\n")[0]
               for ln in prompt.split("---\n")[1:]]
        return verdict_json(ids)

    judge = Judge(settings, caller, state, sleeper=slept.append)
    judge.run(items)
    assert slept, "2リクエスト目の前に待つべき（15 RPM）"
    assert slept[0] <= settings["gemini"]["min_interval_sec"]


# --- プロンプト ---


def test_prompt_includes_all_fields(settings):
    items = make_items(2)
    items[0].body = "本文テキスト"
    prompt = build_prompt(items)
    assert "id: s1" in prompt and "id: s2" in prompt
    assert "機関名: 沖縄県" in prompt
    assert "本文テキスト" in prompt


def test_quota_exceeded_mid_batch_carries_rest(settings, state):
    """バックオフ中に日次上限へ達したら残りを持ち越す。"""
    state.consume_quota(58)
    items = make_items(30)

    def caller(prompt):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    judge = build_judge(settings, caller, state)
    with_verdicts, carried = judge.run(items)
    assert state.quota_count <= 60
    assert len(carried) > 0


def test_relevant_accepts_string_false():
    """モデルが "false" を文字列で返しても False として扱う。"""
    out = validate([{"id": "s1", "relevant": "false"}], ["s1"])
    assert out[0]["relevant"] is False
    out = validate([{"id": "s1", "relevant": "true"}], ["s1"])
    assert out[0]["relevant"] is True
