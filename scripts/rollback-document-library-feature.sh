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

echo "コードをバックアップ時点へ戻しています"
while IFS= read -r relative_path; do
  [[ -n "${relative_path}" ]] || continue
  backup_path="${BACKUP_ROOT}/files/${relative_path}"
  target_path="${TARGET_ROOT}/${relative_path}"
  if [[ -f "${backup_path}" ]]; then
    install -D -m 0644 "${backup_path}" "${target_path}"
  fi
done < "${BACKUP_ROOT}/deploy-file-list.txt"

while IFS= read -r relative_path; do
  [[ -n "${relative_path}" ]] || continue
  target_path="${TARGET_ROOT}/${relative_path}"
  if [[ "${target_path}" == "${TARGET_ROOT}/"* ]]; then
    rm -f -- "${target_path}"
  fi
done < "${BACKUP_ROOT}/new-files.txt"

"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
(
  cd "${TARGET_ROOT}/frontend"
  npm run build
)

stop_processes() {
  local pattern="$1"
  local pids
  pids="$(pgrep -f "${pattern}" || true)"
  [[ -n "${pids}" ]] || return
  kill ${pids}
  sleep 2
}

stop_processes '/u01/aipoc/no.1-semantic-doc-search/backend/.venv/bin/python.*uvicorn app.main:app'
stop_processes 'uv run --directory backend uvicorn app.main:app --host'
stop_processes '/u01/aipoc/no.1-semantic-doc-search/frontend/node_modules/.bin/vite preview'
bash /u01/aipoc/start_semantic_doc_search_services.sh

curl --fail --silent --show-error --retry 30 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:5175/ >/dev/null

echo "コードのロールバックが完了しました"
echo "追加DB表・列は後方互換のため保持しています（旧コードからは参照されません）"
