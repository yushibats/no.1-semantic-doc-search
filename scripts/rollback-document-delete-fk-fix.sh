#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/document-delete-fk-fix}"
readonly BACKUP_ROOT="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0 <backup-path>" >&2
  exit 1
fi
if [[ -z "${BACKUP_ROOT}" || "${BACKUP_ROOT}" != "${BACKUP_PARENT}/"* ]]; then
  echo "正しいバックアップ先を指定してください" >&2
  exit 1
fi
if [[ ! -f "${BACKUP_ROOT}/deploy-file-list.txt" ]]; then
  echo "バックアップのファイル一覧が見つかりません" >&2
  exit 1
fi

while IFS= read -r relative_path; do
  backup_path="${BACKUP_ROOT}/files/${relative_path}"
  if [[ ! -f "${backup_path}" ]]; then
    echo "バックアップファイルが見つかりません: ${relative_path}" >&2
    exit 1
  fi
  install -D -m 0644 "${backup_path}" "${TARGET_ROOT}/${relative_path}"
done < "${BACKUP_ROOT}/deploy-file-list.txt"

"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q   "${TARGET_ROOT}/backend/app"
fuser -k 8081/tcp >/dev/null 2>&1 || true
fuser -k 5175/tcp >/dev/null 2>&1 || true
bash "${TARGET_ROOT}/scripts/start-production-services.sh"

curl --fail --silent --show-error --retry 45 --retry-delay 1   --retry-connrefused --connect-timeout 2   http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 45 --retry-delay 1   --retry-connrefused --connect-timeout 2   http://127.0.0.1:5175/ai/ >/dev/null

echo "削除エラー修正をロールバックしました: ${BACKUP_ROOT}"
