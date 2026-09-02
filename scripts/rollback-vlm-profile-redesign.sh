#!/usr/bin/env bash
set -Eeuo pipefail

readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_ROOT="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0 <backup-directory>" >&2
  exit 1
fi
if [[ -z "${BACKUP_ROOT}" || ! -f "${BACKUP_ROOT}/deploy-file-list.txt" || ! -f "${BACKUP_ROOT}/profiles-before.json" ]]; then
  echo "有効なバックアップディレクトリを指定してください" >&2
  exit 1
fi
case "${BACKUP_ROOT}" in
  /u01/aipoc/backups/vlm-profile-redesign/*) ;;
  *) echo "想定外のバックアップパスです: ${BACKUP_ROOT}" >&2; exit 1 ;;
esac

echo "プロファイル1・2を以前のRevisionへ戻しています"
(
  cd "${TARGET_ROOT}/backend"
  .venv/bin/python - "${BACKUP_ROOT}/profiles-before.json" <<'PY'
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.profile_prompt_migration import restore_profiles

snapshot = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(json.dumps(restore_profiles(snapshot), ensure_ascii=False, default=str))
PY
)

echo "コードをバックアップ時点へ戻しています"
while IFS= read -r relative_path; do
  [[ -n "${relative_path}" ]] || continue
  backup_path="${BACKUP_ROOT}/files/${relative_path}"
  target_path="${TARGET_ROOT}/${relative_path}"
  if [[ ! -f "${backup_path}" || "${target_path}" != "${TARGET_ROOT}/"* ]]; then
    echo "バックアップ内容が不正です: ${relative_path}" >&2
    exit 1
  fi
  install -D -m 0644 "${backup_path}" "${target_path}"
done < <(find "${BACKUP_ROOT}/files" -type f -printf '%P\n')
while IFS= read -r relative_path; do
  [[ -n "${relative_path}" ]] || continue
  target_path="${TARGET_ROOT}/${relative_path}"
  if [[ "${target_path}" != "${TARGET_ROOT}/"* ]]; then
    echo "削除対象が不正です: ${relative_path}" >&2
    exit 1
  fi
  rm -f "${target_path}"
done < "${BACKUP_ROOT}/new-files.txt"

"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
(cd "${TARGET_ROOT}/frontend" && npm run build)
fuser -k 8081/tcp >/dev/null 2>&1 || true
fuser -k 5175/tcp >/dev/null 2>&1 || true
bash "${TARGET_ROOT}/scripts/start-production-services.sh"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

echo "VLMプロファイル再設計のロールバックが完了しました"
