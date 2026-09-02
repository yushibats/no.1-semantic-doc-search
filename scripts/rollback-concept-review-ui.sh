#!/usr/bin/env bash
set -Eeuo pipefail

readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_ROOT="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0 <backup-directory>" >&2
  exit 1
fi
if [[ -z "${BACKUP_ROOT}" || ! -f "${BACKUP_ROOT}/deploy-file-list.txt" ]]; then
  echo "有効なバックアップディレクトリを指定してください" >&2
  exit 1
fi
case "${BACKUP_ROOT}" in
  /u01/aipoc/backups/concept-review-ui/*) ;;
  *) echo "想定外のバックアップパスです: ${BACKUP_ROOT}" >&2; exit 1 ;;
esac

while IFS= read -r relative_path; do
  [[ -n "${relative_path}" ]] || continue
  backup_path="${BACKUP_ROOT}/files/${relative_path}"
  target_path="${TARGET_ROOT}/${relative_path}"
  if [[ ! -f "${backup_path}" || "${target_path}" != "${TARGET_ROOT}/"* ]]; then
    echo "バックアップ内容が不正です: ${relative_path}" >&2
    exit 1
  fi
  install -D -m 0644 "${backup_path}" "${target_path}"
done < "${BACKUP_ROOT}/deploy-file-list.txt"

(cd "${TARGET_ROOT}/frontend" && npm run build)
fuser -k 5175/tcp >/dev/null 2>&1 || true
(
  cd "${TARGET_ROOT}/frontend"
  nohup npm run preview -- --host 0.0.0.0 --port 5175 \
    > /var/log/app-frontend.log 2>&1 &
)
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

echo "コードのロールバックが完了しました"
