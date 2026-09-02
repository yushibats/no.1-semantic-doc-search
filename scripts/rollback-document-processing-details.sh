#!/usr/bin/env bash
set -Eeuo pipefail

readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/document-processing-details}"
readonly BACKUP_ROOT="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0 <backup-root>" >&2
  exit 1
fi
if [[ -z "${BACKUP_ROOT}" || "${BACKUP_ROOT}" != "${BACKUP_PARENT}/"* ]]; then
  echo "正しいバックアップディレクトリを指定してください" >&2
  exit 1
fi
if [[ ! -f "${BACKUP_ROOT}/deployment-complete" || ! -f "${BACKUP_ROOT}/deploy-file-list.txt" ]]; then
  echo "完了済みデプロイのバックアップではありません: ${BACKUP_ROOT}" >&2
  exit 1
fi

while IFS= read -r relative_path; do
  [[ -n "${relative_path}" ]] || continue
  if [[ ! -f "${BACKUP_ROOT}/files/${relative_path}" ]]; then
    echo "バックアップファイルがありません: ${relative_path}" >&2
    exit 1
  fi
  install -D -m 0644 "${BACKUP_ROOT}/files/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done < "${BACKUP_ROOT}/deploy-file-list.txt"

(cd "${TARGET_ROOT}/frontend" && npm run build)
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"

stop_port() {
  local port="$1"
  fuser -k "${port}/tcp" >/dev/null 2>&1 || true
  for _ in {1..20}; do
    if ! fuser "${port}/tcp" >/dev/null 2>&1; then
      return
    fi
    sleep 0.25
  done
  echo "TCP ${port}番の旧プロセスを停止できませんでした" >&2
  return 1
}

stop_port 8081
stop_port 5175
bash "${TARGET_ROOT}/scripts/start-production-services.sh"
curl --fail --silent --show-error --retry 30 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:5175/ >/dev/null

touch "${BACKUP_ROOT}/rolled-back"
echo "処理詳細UIをロールバックしました"
