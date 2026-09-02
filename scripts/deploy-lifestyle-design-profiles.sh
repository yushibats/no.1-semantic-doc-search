#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/lifestyle-design-profiles}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  backend/app/rag/profile_prompts.py
  backend/app/rag/profile_prompt_migration.py
  backend/app/rag/pipeline_engine.py
)
DB_MUTATED=0

expected_target_hash() {
  case "$1" in
    backend/app/rag/profile_prompts.py) printf '%s' '0a1405069ef30bb9e1d869356a760884e0d2c761b5d186ccc7ca93364b05eef2' ;;
    backend/app/rag/profile_prompt_migration.py) printf '%s' 'e3c74b4f8c63ea13b894c73de9fe64e959d960fedbf55e9fea9cba0912816f18' ;;
    backend/app/rag/pipeline_engine.py) printf '%s' '817b0d99764dbcec744ad36aedc083992fca381403b132779779f3abc8b822a2' ;;
    *) return 1 ;;
  esac
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0" >&2
  exit 1
fi
if [[ ! -d "${SOURCE_ROOT}/.git" || ! -d "${TARGET_ROOT}/.git" ]]; then
  echo "SOURCE_ROOTまたはTARGET_ROOTがリポジトリではありません" >&2
  exit 1
fi
if [[ ! -x "${TARGET_ROOT}/backend/.venv/bin/python" ]]; then
  echo "稼働先のbackend仮想環境が見つかりません" >&2
  exit 1
fi
if [[ ! -f "${TARGET_ROOT}/backend/app/rag/concept_prompt_migration.py" ]]; then
  echo "稼働先にAI検索候補の移行機能がないため中止します" >&2
  exit 1
fi

source_commit="$(git -C "${SOURCE_ROOT}" rev-parse HEAD)"
target_commit="$(git -c safe.directory="${TARGET_ROOT}" -C "${TARGET_ROOT}" rev-parse HEAD)"
if [[ "${source_commit}" != "${target_commit}" ]]; then
  echo "作業元と稼働先のGit基点が異なるため中止します" >&2
  exit 1
fi

for relative_path in "${DEPLOY_FILES[@]}"; do
  source_path="${SOURCE_ROOT}/${relative_path}"
  target_path="${TARGET_ROOT}/${relative_path}"
  [[ -f "${source_path}" && -f "${target_path}" ]] || {
    echo "対象ファイルが見つかりません: ${relative_path}" >&2
    exit 1
  }
  source_hash="$(sha256sum "${source_path}" | awk '{print $1}')"
  target_hash="$(sha256sum "${target_path}" | awk '{print $1}')"
  expected_hash="$(expected_target_hash "${relative_path}")"
  if [[ "${target_hash}" != "${source_hash}" && "${target_hash}" != "${expected_hash}" ]]; then
    echo "稼働先が確認時点から変更されているため、上書きせず中止します: ${relative_path}" >&2
    exit 1
  fi
done

echo "[1/7] 生活イメージ・デザイン抽出の対象テストを実行しています"
(
  cd "${SOURCE_ROOT}/backend"
  PYTHONPATH=. "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q     tests/test_rag_profiles.py::test_recommended_prompts_extract_supported_lifestyle_search_phrases     tests/test_rag_profiles.py::test_specialized_profiles_cover_samples_and_guard_room_calculation     tests/test_pipeline_runtime.py::test_search_concept_taxonomy_has_a_lifestyle_category
)

echo "[2/7] コード・プロファイル1/3・AI検索候補設定をバックアップしています: ${BACKUP_ROOT}"
mkdir -p "${BACKUP_ROOT}/files"
printf '%s\n' "${DEPLOY_FILES[@]}" > "${BACKUP_ROOT}/deploy-file-list.txt"
: > "${BACKUP_ROOT}/new-files.txt"
for relative_path in "${DEPLOY_FILES[@]}"; do
  backup_path="${BACKUP_ROOT}/files/${relative_path}"
  mkdir -p "$(dirname "${backup_path}")"
  cp -a "${TARGET_ROOT}/${relative_path}" "${backup_path}"
done
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

from app.rag.document_metadata_repository import document_metadata_repository
from app.rag.profile_repository import profile_repository

concept = document_metadata_repository.get_concept_settings()
payload = {
    "profiles": [
        profile_repository.get_profile(slot).model_dump(mode="json")
        for slot in (1, 3)
    ],
    "concept": {
        "prompt_text": concept.prompt_text,
        "taxonomy_revision": concept.taxonomy_revision,
    },
}
Path(sys.argv[1]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
    encoding="utf-8",
)
PY
)
chmod 0600 "${BACKUP_ROOT}/settings-before.json"
printf '{"job_ids":[]}\n' > "${BACKUP_ROOT}/jobs-created.json"
chmod 0600 "${BACKUP_ROOT}/jobs-created.json"

cancel_created_jobs() {
  [[ -f "${BACKUP_ROOT}/jobs-created.json" ]] || return 0
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
}

restore_code() {
  while IFS= read -r relative_path; do
    [[ -n "${relative_path}" ]] || continue
    backup_path="${BACKUP_ROOT}/files/${relative_path}"
    target_path="${TARGET_ROOT}/${relative_path}"
    [[ -f "${backup_path}" && "${target_path}" == "${TARGET_ROOT}/"* ]] || continue
    install -D -m 0644 "${backup_path}" "${target_path}"
  done < "${BACKUP_ROOT}/deploy-file-list.txt"
}

restore_database_settings() {
  [[ "${DB_MUTATED}" -eq 1 ]] || return 0
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
}

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  echo "反映に失敗したため、新規Job・設定・コードを自動復元します" >&2
  cancel_created_jobs || true
  restore_database_settings || true
  restore_code
  "${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app" || true
  fuser -k 8081/tcp >/dev/null 2>&1 || true
  fuser -k 5175/tcp >/dev/null 2>&1 || true
  bash "${TARGET_ROOT}/scripts/start-production-services.sh" || true
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[3/7] プロファイルとAI検索候補のコードを反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[4/7] 稼働先の構文を検証しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"

echo "[5/7] バックエンドとフロントエンドを再起動しています"
fuser -k 8081/tcp >/dev/null 2>&1 || true
fuser -k 5175/tcp >/dev/null 2>&1 || true
bash "${TARGET_ROOT}/scripts/start-production-services.sh"

echo "[6/7] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

echo "[7/7] プロファイル1・3とAI検索候補を適用し、既存文書の再抽出Jobを登録しています"
DB_MUTATED=1
(
  cd "${TARGET_ROOT}/backend"
  .venv/bin/python - "${BACKUP_ROOT}/jobs-created.json" <<'PY'
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.concept_prompt_migration import apply_recommended_concept_prompt
from app.rag.profile_prompt_migration import apply_design_profiles
from app.rag.profile_repository import profile_repository
from app.rag.settings_api import apply_profile as apply_and_queue_profile

jobs_path = Path(sys.argv[1])
result = {
    "profiles": apply_design_profiles(),
    "concept": apply_recommended_concept_prompt(),
    "queued": [],
    "job_ids": [],
}
jobs_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
for slot_no in (1, 3):
    queued = apply_and_queue_profile(
        slot_no,
        profile_repository.get_profile(slot_no),
        BackgroundTasks(),
        run_vlm=True,
    )
    result["queued"].append(queued)
    result["job_ids"].extend(queued.get("job_ids") or [])
    jobs_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
print(json.dumps(result, ensure_ascii=False, default=str))
PY
)

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "生活イメージ・デザイン抽出プロファイルの反映が完了しました"
echo "プロファイル1・3の既存文書再抽出Jobを登録しました。新規文書にも自動適用されます。"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-lifestyle-design-profiles.sh ${BACKUP_ROOT}"
