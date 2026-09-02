#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="/home/ubuntu/projects/my-project/no.1-semantic-doc-search"
TARGET_ROOT="/u01/aipoc/no.1-semantic-doc-search"
BACKUP_PARENT="/u01/aipoc/backups/document-list-sorting"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"

DEPLOY_FILES=(
  "backend/app/rag/document_metadata_api.py"
  "backend/app/rag/document_metadata_repository.py"
  "frontend/index.html"
  "frontend/app.js"
  "frontend/src/modules/document-library.js"
  "frontend/src/style.css"
)

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限が必要です。sudo bash $0 を実行してください。" >&2
  exit 1
fi

for relative_path in "${DEPLOY_FILES[@]}"; do
  if [[ ! -f "${SOURCE_ROOT}/${relative_path}" ]]; then
    echo "反映元ファイルがありません: ${SOURCE_ROOT}/${relative_path}" >&2
    exit 1
  fi
  if [[ ! -f "${TARGET_ROOT}/${relative_path}" ]]; then
    echo "稼働先ファイルがありません: ${TARGET_ROOT}/${relative_path}" >&2
    exit 1
  fi
done

if [[ ! -x "${TARGET_ROOT}/backend/.venv/bin/python" ]]; then
  echo "本番Python環境が見つかりません。" >&2
  exit 1
fi

mkdir -p "${BACKUP_ROOT}/files"
for relative_path in "${DEPLOY_FILES[@]}"; do
  mkdir -p "${BACKUP_ROOT}/files/$(dirname "${relative_path}")"
  cp -a "${TARGET_ROOT}/${relative_path}" "${BACKUP_ROOT}/files/${relative_path}"
done
printf '%s\n' "${DEPLOY_FILES[@]}" > "${BACKUP_ROOT}/deploy-file-list.txt"

rollback_on_error() {
  local exit_code="${1:-$?}"
  echo "反映に失敗したため、バックアップから自動復元します。" >&2
  while IFS= read -r relative_path; do
    install -m "$(stat -c '%a' "${BACKUP_ROOT}/files/${relative_path}")" \
      "${BACKUP_ROOT}/files/${relative_path}" "${TARGET_ROOT}/${relative_path}"
  done < "${BACKUP_ROOT}/deploy-file-list.txt"
  "${TARGET_ROOT}/backend/.venv/bin/python" -m py_compile \
    "${TARGET_ROOT}/backend/app/rag/document_metadata_api.py" \
    "${TARGET_ROOT}/backend/app/rag/document_metadata_repository.py" || true
  (cd "${TARGET_ROOT}/frontend" && npm run build) || true
  fuser -k 8081/tcp 2>/dev/null || true
  fuser -k 5175/tcp 2>/dev/null || true
  bash "${TARGET_ROOT}/scripts/start-production-services.sh" || true
  echo "自動復元先: ${BACKUP_ROOT}" >&2
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[1/5] 稼働中ファイルをバックアップしました: ${BACKUP_ROOT}"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -m "$(stat -c '%a' "${TARGET_ROOT}/${relative_path}")" \
    "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[2/5] バックエンドの構文を検証しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m py_compile \
  "${TARGET_ROOT}/backend/app/rag/document_metadata_api.py" \
  "${TARGET_ROOT}/backend/app/rag/document_metadata_repository.py"

echo "[3/5] フロントエンドを本番ビルドしています"
(cd "${TARGET_ROOT}/frontend" && npm run build)

echo "[4/5] バックエンドとフロントエンドを再起動しています"
fuser -k 8081/tcp 2>/dev/null || true
fuser -k 5175/tcp 2>/dev/null || true
bash "${TARGET_ROOT}/scripts/start-production-services.sh"

echo "[5/5] ヘルスチェックを実行しています"
backend_ready=false
frontend_ready=false
for _ in $(seq 1 45); do
  backend_status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 \
    "http://127.0.0.1:8081/health" 2>/dev/null || true)"
  frontend_status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 \
    "http://127.0.0.1:5175/ai/" 2>/dev/null || true)"
  if [[ "${backend_status}" == "200" ]]; then
    backend_ready=true
  fi
  if [[ "${frontend_status}" == "200" ]]; then
    frontend_ready=true
  fi
  if [[ "${backend_ready}" == true && "${frontend_ready}" == true ]]; then
    break
  fi
  sleep 2
done

if [[ "${backend_ready}" != true ]]; then
  echo "バックエンドのヘルスチェックに失敗しました（最終HTTP: ${backend_status:-接続不可}）。" >&2
  tail -n 80 /var/log/app-backend.log >&2 2>/dev/null || true
  rollback_on_error 1
fi
if [[ "${frontend_ready}" != true ]]; then
  echo "フロントエンドのヘルスチェックに失敗しました（最終HTTP: ${frontend_status:-接続不可}）。" >&2
  tail -n 80 /var/log/app-frontend.log >&2 2>/dev/null || true
  rollback_on_error 1
fi

grep -R -F -q "更新日時の新しい順" "${TARGET_ROOT}/frontend/dist/assets"
grep -R -F -q "文書一覧を更新しました" "${TARGET_ROOT}/frontend/dist/assets"
touch "${BACKUP_ROOT}/deployment-complete"
trap - ERR

echo "文書一覧の並び替え・年月配置・更新表記をデプロイしました。"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-document-list-sorting.sh ${BACKUP_ROOT}"
