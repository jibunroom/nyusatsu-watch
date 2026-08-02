# nyusatsu-watch

沖縄本島の自治体・国の機関が公開する入札・公募情報を毎日自動巡回し、
自社（IT・事務効率化・制作）が取れる案件だけをメールで通知する。

- 実行基盤: GitHub Actions のみ（サーバー不要）
- 言語: Python 3.12
- 通知: お名前.com SMTP → 個人Gmail
- AI: Gemini API 無料ティア（一次フィルタ通過分のみ・バッチ判定）

## パイプライン

```
fetch → dedupe → prefilter → detail → judge → rank → notify → persist
```

一次フィルタ（AI不使用）で大半を落としてから Gemini に渡すのが要。
実測（2026-08-01・23機関）では **867件の新着 → 107件が一次通過**で、
**約88%を1トークンも使わずに除去**できている。
107件なら Gemini 11リクエスト（上限30/実行・60/日）に収まる。

これは seen.json が空の初回の数字。2回目以降は重複排除が効くため、
1回あたりの判定対象はさらに小さくなる。

## 監視状況（2026-08-01 時点）

| | 機関数 |
|---|---|
| フィード巡回中（`status: ok`） | 23 |
| フィード未発見（`status: no_feed`） | 4 — 南城市 / 恩納村 / 宜野座村 / 中城村 |
| スクレイピング枠・v1未実装（`method: scrape`） | 2 — 沖縄総合事務局 / 沖縄防衛局 |
| 合計 | 29 |

未発見の4村市は RSS/Atom を公開していないことを実際に確認済み
（`<link rel=alternate>`・アンカー・よくあるパス14種すべて不発）。
サイトがリニューアルされれば週次の `discover.yml` が自動で拾う。

## セットアップ

### 1. ローカル

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # 値を埋める
```

### 2. GitHub Secrets（§8-3）

リポジトリの Settings → Secrets and variables → Actions に登録する。
コードには一切書かない。

| 種別 | 名前 |
|---|---|
| Secret | `GEMINI_API_KEY` |
| Secret | `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` |
| Secret | `MAIL_TO` |
| Variable | `GEMINI_MODEL`（任意。未設定なら `config/settings.yml` の既定値） |

`watch.yml` は `data/` をコミットするので、Settings → Actions → General →
Workflow permissions を **Read and write permissions** にしておく。

### 3. 動作確認（この順で）

```bash
.venv/bin/python -m pytest -q
```

```bash
MAIL_TO=your@example.com .venv/bin/python -m src.main --dry-run --no-ai
```

```bash
.venv/bin/python -m src.main --dry-run --limit 1
```

いきなり全機関・全件で本番実行しない。

## 実行モード

| コマンド | 挙動 |
|---|---|
| `python -m src.main` | 本番。メール送信 + git push あり |
| `python -m src.main --dry-run` | メール送信と git push をスキップし標準出力に表示 |
| `python -m src.main --limit 1` | Gemini 実呼び出しを1件だけ行う |
| `python -m src.main --no-ai` | Gemini を一切呼ばない（全件A枠で出力） |
| `python -m src.discover` | フィード自動探索。`config/sources.yml` を更新 |
| `python -m src.discover --dry-run` | 探索するが sources.yml を書き換えない |

## ワークフロー

| ファイル | 実行 | 内容 |
|---|---|---|
| `.github/workflows/watch.yml` | JST 07:37 / 17:37 + 手動 | 本体 |
| `.github/workflows/discover.yml` | 毎週月曜 JST 06:00 + 手動 | フィード探索 |
| `.github/workflows/ci.yml` | push / PR | pytest |

`watch.yml` は毎回 `data/` をコミットする。これが
「60日間コミットが無いとスケジュールが無効化される」GitHub の仕様への
対策を兼ねている。

実行時刻の分が 37 なのは意図的。GitHub の schedule は 0分・30分に予約が
集中し、混雑で遅延・欠落しやすい（2026-08-01 の初日 JST 17:30 分が実際に
欠落した）ため、混雑しない分にずらしてある。

## 無料枠の守り方（§4-1）

| 項目 | 値 |
|---|---|
| バッチ | 1リクエストに最大10件 |
| 間隔 | リクエスト間 最低4.1秒（15 RPM） |
| 1実行の上限 | 30リクエスト（=最大300件） |
| 1日の上限 | 60リクエスト（朝夕合算・`data/quota.json`） |
| リトライ | 429/503 は 8→16→32秒、最大3回 |

上限に達したら Gemini を呼ばずに終了し、未判定分は `data/pending.json`
に積んで次回実行の先頭で消化する。**判定を落とすのではなく遅らせる。**

## ランク（§5-1）

| ランク | 意味 | 通知 |
|---|---|---|
| S | 資格の壁が無く締切に余裕がある本命 | する |
| A | 資格・実績・地域要件などの壁あり / 締切7日未満 / AI判定失敗 | する |
| B | AI が対象外と判定（記録のみ） | しない |
| 除外 | 金額1000万円超 / 重い実績要件の大型プロポーザル | しない |

- `staff_required >= 5` は除外せず件名に ⚠️ を付ける
- 0件でも毎回メールを送る（生存通知）

### S と A の境界について

仕様の S 行「(資格不要 **or** 随意契約)」と A 行「入札参加資格の壁がある」は
そのままだと重なる。随意契約には入札参加資格審査が無いため、
**随意契約のときだけ資格の壁を打ち消す**という読みで実装した。
実績要件・地域要件・Pマーク等の壁は随意契約でも残り、A に落ちる。
（`src/rank.py` の `_has_barrier`）

締切が判明しなかった案件は S ではなく A に入れる。
S の条件が「締切まで7日以上」であることを確認できないため。

## 運用メモ

### 落札結果（§6）の取りこぼしについて

`config/filters.yml` の `result_markers` は仕様どおり
`落札 / 入札結果 / 開札結果 / 契約結果` の4語にしてある。

ただし実測（2026-08-01・23機関867件）でこの4語に当たったのは
**わずか1件**だった。実際の feed には次の言い回しで載っていることが多い:

- 「公募型プロポーザル**選定結果**について」（宜野湾市）
- 「公募型プロポーザルの**審査結果**について」（那覇市）

結果を積極的に集めたい場合は `result_markers` に `選定結果` `審査結果` を
足す。ただし **`result_markers` に当たった案件は Gemini 判定に乗らない**
（§2-3）ので、足しすぎると本命案件を取りこぼす。まず `data/items.json` を
数週間ためて、実際の取りこぼし具合を見てから調整するのがよい。

### フィルタ語彙の調整

`config/filters.yml` はコードを触らずに調整できる。

- `include` … これに当たらないものは Gemini に行かない。緩めに保つ
- `exclude` … タイトルだけで確実に切れるものだけ入れる
- `exclude_cooccurrence` / `exclude_rescue` … 「実施設計」等が
  「システム設計」を巻き込まないための共起ルール

迷うものは除外せず Gemini に回す（取りこぼし防止優先）。

### 監視先の追加

`config/sources.yml` に `name` / `top` / `method: feed` / `status: todo` を
足すだけでよい。次回の `discover.yml` がフィードを探して `status: ok` に
書き換える。離島市町村もこれで足せる。

`discover.py` は `status: ok` の機関でも、日次実行でフィード取得が
**3回連続失敗**（`fail_streak`）していたら再探索する。サイトリニューアルに
自動で追従するため。

## 状態ファイル（§7・コミット対象）

| ファイル | 内容 |
|---|---|
| `data/seen.json` | 取得済みURL（機関ごと・最大10,000件でローテーション） |
| `data/pending.json` | Gemini 未判定の持ち越し |
| `data/results.json` | 落札結果 |
| `data/quota.json` | 当日の Gemini 使用数 |
| `data/items.json` | 判定済み案件の全記録（S/A/B・分析用） |
| `data/undelivered.json` | メールを送れなかった S/A。送信成功まで消えない |

### 通知の取りこぼし防止

「判定した」ではなく「**実際にメールが届いた**」までを完了とみなす。
送信に失敗した S/A は `data/undelivered.json` に残り、次回の実行で
必ず本文に載せ直される（締切を過ぎたものだけ落とす）。

2026-08-01 に、お名前.com の海外送信制限でメール送信が失敗した回の
A判定23件が、判定済み扱いのまま二度と通知されない状態になった。
その再発防止。

## テスト

```bash
.venv/bin/python -m pytest -q
```

- `tests/fixtures/` に実フィード4件と実案件ページ3件を保存し、
  **ネット・Gemini・SMTP に一切触れずに** fetch〜prefilter〜results〜notify
  まで通す
- `tests/test_judge.py` は Gemini 呼び出しを差し替え、
  正常JSON / 壊れたJSON / id欠落 の3ケースで検証ロジックを試す

## v1 で実装していないもの（§13）

- 内閣府沖縄総合事務局・沖縄防衛局のスクレイピング
  （`sources.yml` に `method: scrape` / `status: todo` で登録だけしてある）
- PDF の中身の読み取り。リンク先が PDF の場合はページ上のテキストだけで
  判定し、通知に `[PDFのみ]` と明記する
- 落札結果の機関横断ランキング自動生成

## ディレクトリ

```
src/
├── main.py        エントリポイント（パイプライン統括）
├── config.py      config/*.yml と .env の読み込み
├── fetch.py       フィード取得・本文抽出・巡回マナー
├── discover.py    フィード自動探索
├── prefilter.py   一次フィルタ
├── judge.py       Gemini 判定（バッチ・検証・バックオフ）
├── rank.py        S/A/B 分類
├── results.py     落札結果抽出
├── notify.py      メール生成・送信
└── state.py       data/*.json の読み書き・quota管理
```

`config.py` と `rank.py` は仕様§10の一覧に無いが、
パス解決（§10）とランク付け（§0 の `[rank]` 工程）をそれぞれ単独で
テストできるように分けた。
