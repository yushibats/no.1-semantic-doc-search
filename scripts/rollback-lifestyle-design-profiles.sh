#!/usr/bin/env bash
set -Eeuo pipefail

readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_ROOT="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0 <backup-directory>" >&2
  exit 1
fi
if [[ -z "${BACKUP_ROOT}" || ! -f "${BACKUP_ROOT}/deploy-file-list.txt" || ! -f "${BACKUP_ROOT}/settings-before.json" ]]; then
  echo "有効なバックアップディレクトリを指定してください" >&2
  exit 1
fi
case "${BACKUP_ROOT}" in
  /u01/aipoc/backups/lifestyle-design-profiles/*) ;;
  *) echo "想定外のバックアップパスです: ${BACKUP_ROOT}" >&2; exit 1 ;;
esac

echo "今回登録した未完了Jobを停止しています"
if [[ -f "${BACKUP_ROOT}/jobs-created.json" ]]; then
  (
    cd "${TARGET_ROOT}/backend"
    .venv/bin/python - "${BACKUP_ROOT}/jobs-created.json" <<'PY'
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.pipeline_repository import pipeline_repository

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for job_id in payload.get("job_ids", []):
    pipeline_repository.cancel_job(str(job_id))
PY
  )
fi

echo "プロファイル1・3とAI検索候補設定を以前の状態へ戻しています"
(
  cd "${TARGET_ROOT}/backend"
  .venv/bin/python - "${BACKUP_ROOT}/settings-before.json" <<'PY'
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.concept_prompt_migration import restore_concept_prompt
from app.rag.profile_prompt_migration import restore_design_profiles

snapshot = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(json.dumps(restore_design_profiles(snapshot["profiles"]), ensure_ascii=False, default=str))
print(json.dumps(restore_concept_prompt(snapshot["concept"]), ensure_ascii=False, default=str))
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
done < "${BACKUP_ROOT}/deploy-file-list.txt"

"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
fuser -k 8081/tcp >/dev/null 2>&1 || true
fuser -k 5175/tcp >/dev/null 2>&1 || true
bash "${TARGET_ROOT}/scripts/start-production-services.sh"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

echo "生活イメージ・デザイン抽出プロファイルのロールバックが完了しました"
