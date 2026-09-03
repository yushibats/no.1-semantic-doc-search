#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_ROOT="${APP_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/typed-building-query}"
readonly BACKUP_ROOT="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0 <backup-directory>" >&2
  exit 1
fi
case "${BACKUP_ROOT}" in
  "${BACKUP_PARENT}/"*) ;;
  *) echo "想定外のバックアップパスです: ${BACKUP_ROOT}" >&2; exit 1 ;;
esac
if [[ ! -f "${BACKUP_ROOT}/deployment-complete" || \
      ! -f "${BACKUP_ROOT}/restore-file-list.txt" || \
      ! -x "${APP_ROOT}/backend/.venv/bin/python" ]]; then
  echo "完了済みバックアップまたは稼働先Python環境が見つかりません" >&2
  exit 1
fi

echo "[1/5] VLMとAI検索候補プロンプトを復元しています"
shopt -s nullglob
prompt_backups=("${BACKUP_ROOT}/prompt-settings"/*)
if [[ "${#prompt_backups[@]}" -gt 1 ]]; then
  echo "プロンプトのバックアップが複数あり、復元対象を特定できません" >&2
  exit 1
fi
if [[ "${#prompt_backups[@]}" -eq 1 ]]; then
  env BACKUP_PARENT="${BACKUP_ROOT}/prompt-settings" APP_ROOT="${APP_ROOT}" \
    bash "${APP_ROOT}/scripts/rollback-search-ready-vlm-prompts.sh" \
    "${prompt_backups[0]}"
fi

echo "[2/5] 更新前コードを復元しています"
while IFS= read -r relative_path; do
  [[ -n "${relative_path}" ]] || continue
  install -D -m 0644 \
    "${BACKUP_ROOT}/files/${relative_path}" \
    "${APP_ROOT}/${relative_path}"
done < "${BACKUP_ROOT}/restore-file-list.txt"
while IFS= read -r relative_path; do
  [[ -n "${relative_path}" ]] || continue
  rm -f -- "${APP_ROOT}/${relative_path}"
done < "${BACKUP_ROOT}/new-file-list.txt"

echo "[3/5] 復元コードを検証してフロントエンドを再構築しています"
"${APP_ROOT}/backend/.venv/bin/python" -m compileall -q \
  "${APP_ROOT}/backend/app"
(cd "${APP_ROOT}/frontend" && npm run build)

echo "[4/5] サービスを再起動しています"
fuser -k 8081/tcp >/dev/null 2>&1 || true
fuser -k 5175/tcp >/dev/null 2>&1 || true
bash "${APP_ROOT}/scripts/start-production-services.sh"

echo "[5/5] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 60 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 60 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:5175/ai/ >/dev/null

echo "自然言語条件解析・型付き面積属性・検索フィルタをロールバックしました"
echo "追加スキーマは後方互換のため残しています。既存データは削除していません。"
