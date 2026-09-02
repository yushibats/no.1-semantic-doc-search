#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_ROOT="/u01/aipoc/no.1-semantic-doc-search"
BACKUP_ROOT="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限が必要です。sudo bash $0 <backup-directory> を実行してください。" >&2
  exit 1
fi
if [[ -z "${BACKUP_ROOT}" || ! -f "${BACKUP_ROOT}/deploy-file-list.txt" ]]; then
  echo "有効なバックアップディレクトリを指定してください。" >&2
  exit 1
fi
case "${BACKUP_ROOT}" in
  /u01/aipoc/backups/document-list-sorting/*) ;;
  *) echo "想定外のバックアップパスです: ${BACKUP_ROOT}" >&2; exit 1 ;;
esac

while IFS= read -r relative_path; do
  if [[ ! -f "${BACKUP_ROOT}/files/${relative_path}" ]]; then
    echo "バックアップファイルがありません: ${relative_path}" >&2
    exit 1
  fi
  install -m "$(stat -c '%a' "${BACKUP_ROOT}/files/${relative_path}")" \
    "${BACKUP_ROOT}/files/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done < "${BACKUP_ROOT}/deploy-file-list.txt"

"${TARGET_ROOT}/backend/.venv/bin/python" -m py_compile \
  "${TARGET_ROOT}/backend/app/rag/document_metadata_api.py" \
  "${TARGET_ROOT}/backend/app/rag/document_metadata_repository.py"
(cd "${TARGET_ROOT}/frontend" && npm run build)
fuser -k 8081/tcp 2>/dev/null || true
fuser -k 5175/tcp 2>/dev/null || true
bash "${TARGET_ROOT}/scripts/start-production-services.sh"

echo "ロールバックが完了しました: ${BACKUP_ROOT}"
