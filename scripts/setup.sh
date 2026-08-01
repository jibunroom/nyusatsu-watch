#!/bin/bash
# .env の値を検証 → GitHub Secrets に登録 → Gemini 1件テスト、までを一気にやる。
# 使い方: .env の3箇所を埋めてから  bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="jibunroom/nyusatsu-watch"

# --- 1. .env を読む ---
if [ ! -f .env ]; then
  echo "❌ .env がありません。.env.example をコピーして作ってください。"
  exit 1
fi
set -a; source .env; set +a

ng=0
check() {
  local name="$1" val="$2"
  if [ -z "$val" ] || [ "$val" = "ここに書く" ]; then
    echo "❌ .env の $name がまだ書かれていません"
    ng=1
  else
    echo "✅ $name OK"
  fi
}
echo "―― .env の確認 ――"
check GEMINI_API_KEY "${GEMINI_API_KEY:-}"
check SMTP_USER "${SMTP_USER:-}"
check SMTP_PASS "${SMTP_PASS:-}"
if [ "$ng" = 1 ]; then
  echo ""
  echo ".env を開いて「ここに書く」の部分を実際の値に置き換えて、保存してから"
  echo "もう一度このスクリプトを実行してください。"
  exit 1
fi

# --- 2. GitHub Secrets に登録 ---
echo ""
echo "―― GitHub Secrets への登録 ――"
printf '%s' "$GEMINI_API_KEY" | gh secret set GEMINI_API_KEY --repo "$REPO"
printf '%s' "$SMTP_USER"      | gh secret set SMTP_USER      --repo "$REPO"
printf '%s' "$SMTP_PASS"      | gh secret set SMTP_PASS      --repo "$REPO"
echo "✅ 3件を登録しました"
gh secret list --repo "$REPO"

# --- 3. Gemini を1件だけ呼ぶテスト（メール送信なし・課金なし） ---
echo ""
echo "―― Gemini 1件テスト（--dry-run --limit 1）――"
echo "   ※ 全機関の巡回を含むため数分かかります。そのままお待ちください。"
.venv/bin/python -m src.main --dry-run --limit 1

echo ""
echo "🎉 ここまでエラーが無ければセットアップ完了です。"
