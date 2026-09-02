#!/usr/bin/env bash
set -Eeuo pipefail

readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_ROOT="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0 <backup-directory>" >&2
  exit 1
fi
case "${BACKUP_ROOT}" in
  /u01/aipoc/backups/lifestyle-search-concepts/*) ;;
  *) echo "想定外のバックアップパスです: ${BACKUP_ROOT}" >&2; exit 1 ;;
esac
if [[ ! -f "${BACKUP_ROOT}/settings-before.json" || ! -f "${BACKUP_ROOT}/deploy-file-list.txt" ]]; then
  echo 'バックアップが不完全です' >&2
  exit 1
fi

echo '[1/4] VLMプロファイルとAIコンセプト設定を復元しています'
(
  cd "${TARGET_ROOT}/backend"
  .venv/bin/python - "${BACKUP_ROOT}/settings-before.json" <<'PY'
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path('..') / '.env', override=False)
client_dir = os.getenv('ORACLE_CLIENT_LIB_DIR', '')
if not os.getenv('TNS_ADMIN') and client_dir:
    os.environ['TNS_ADMIN'] = str(Path(client_dir) / 'network' / 'admin')

from app.rag.concept_prompt_migration import restore_concept_prompt
from app.rag.profile_prompt_migration import restore_profiles

snapshot = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
restore_profiles(snapshot['profiles'])
restore_concept_prompt(snapshot['concept'])
PY
)

echo '[2/4] コードを復元しています'
while IFS= read -r relative_path; do
  [[ -n "${relative_path}" ]] || continue
  backup_path="${BACKUP_ROOT}/files/${relative_path}"
  if [[ -f "${backup_path}" ]]; then
    install -D -m 0644 "${backup_path}" "${TARGET_ROOT}/${relative_path}"
  fi
done < "${BACKUP_ROOT}/deploy-file-list.txt"
while IFS= read -r relative_path; do
  [[ -n "${relative_path}" ]] || continue
  target_path="${TARGET_ROOT}/${relative_path}"
  [[ "${target_path}" == "${TARGET_ROOT}/"* ]] || { echo '不正なパスです' >&2; exit 1; }
  rm -f "${target_path}"
done < "${BACKUP_ROOT}/new-files.txt"

"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"

echo '[3/4] サービスを再起動しています'
fuser -k 8081/tcp >/dev/null 2>&1 || true
fuser -k 5175/tcp >/dev/null 2>&1 || true
bash "${TARGET_ROOT}/scripts/start-production-services.sh"

echo '[4/4] ヘルスチェックを実行しています'
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

echo '生活イメージ対応AIコンセプト抽出をロールバックしました'
