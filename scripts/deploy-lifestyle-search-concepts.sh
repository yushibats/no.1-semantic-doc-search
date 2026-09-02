#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/lifestyle-search-concepts}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  backend/app/rag/pipeline_engine.py
  backend/app/rag/document_metadata_schema.py
  backend/app/rag/profile_prompts.py
  backend/app/rag/concept_prompt_migration.py
  backend/app/rag/document_metadata_repository.py
  backend/app/rag/search_api.py
)
DB_MUTATED=0

expected_target_hash() {
  case "$1" in
    backend/app/rag/pipeline_engine.py) printf '%s' '87c9d42b53b281dff1b901bc1cbf967c03b9abcd4dda83d5a4288520f1534b00' ;;
    backend/app/rag/document_metadata_schema.py) printf '%s' 'f780f2ddcaa808c7f4a247f413c07d81f0c71cc4bf986211aa8cfdef8541bcdb' ;;
    backend/app/rag/profile_prompts.py) printf '%s' '8281ff12bcfc66c4a3cb11aa8a4348fde46b6f4f549e101b25fdb0ad9b8b40cc' ;;
    backend/app/rag/concept_prompt_migration.py) printf '%s' 'ABSENT' ;;
    backend/app/rag/document_metadata_repository.py) printf '%s' 'b25c960b053f8c6e3a77f5d37cabb2a9a095fa88d7a981d462d462c106ad33ed' ;;
    backend/app/rag/search_api.py) printf '%s' 'a986d1a02bc4b2d599dc911ffa30d8202eea98a11968cd933bcf5b54ad060f0d' ;;
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
source_commit="$(git -C "${SOURCE_ROOT}" rev-parse HEAD)"
target_commit="$(git -c safe.directory="${TARGET_ROOT}" -C "${TARGET_ROOT}" rev-parse HEAD)"
if [[ "${source_commit}" != "${target_commit}" ]]; then
  echo "作業元と稼働先のGit基点が異なるため中止します" >&2
  exit 1
fi

for relative_path in "${DEPLOY_FILES[@]}"; do
  source_path="${SOURCE_ROOT}/${relative_path}"
  target_path="${TARGET_ROOT}/${relative_path}"
  expected_hash="$(expected_target_hash "${relative_path}")"
  [[ -f "${source_path}" ]] || { echo "反映元ファイルがありません: ${relative_path}" >&2; exit 1; }
  if [[ -f "${target_path}" ]]; then
    source_hash="$(sha256sum "${source_path}" | awk '{print $1}')"
    target_hash="$(sha256sum "${target_path}" | awk '{print $1}')"
    if [[ "${target_hash}" != "${source_hash}" && "${target_hash}" != "${expected_hash}" && ! ( "${relative_path}" == "backend/app/rag/pipeline_engine.py" && "${target_hash}" == "7050dfe9f427da90543ab522f04949ca11782a09ac4a9b01a1c5229733f0b0aa" ) ]]; then
      echo "稼働先が確認時点から変更されているため中止します: ${relative_path}" >&2
      exit 1
    fi
  elif [[ "${expected_hash}" != 'ABSENT' ]]; then
    echo "稼働先ファイルが見つからないため中止します: ${relative_path}" >&2
    exit 1
  fi
done

echo '[1/7] 生活イメージ・VLM・追加Jobの対象限定テストを実行しています'
(
  cd "${SOURCE_ROOT}/backend"
  PYTHONPATH=. "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q \
    tests/test_rag_profiles.py::test_recommended_prompts_extract_supported_lifestyle_search_phrases \
    tests/test_pipeline_runtime.py::test_search_concept_taxonomy_has_a_lifestyle_category \
    tests/test_pipeline_runtime.py::test_concept_prompt_migration_preserves_thresholds_and_bumps_taxonomy \
    tests/test_pipeline_runtime.py::test_concept_only_job_objects_detects_read_only_retry_targets \
    tests/test_pipeline_runtime.py::test_concept_only_context_reuses_release_without_creating_draft \
    tests/test_pipeline_runtime.py::test_repair_stranded_concept_only_draft_restores_indexed_document \
    tests/test_pipeline_runtime.py::test_empty_out_of_scope_vlm_output_is_stored_as_a_successful_artifact \
    tests/test_document_metadata_feature.py::test_search_candidate_list_excludes_zero_support_when_requested \
    tests/test_document_metadata_feature.py::test_document_concept_aggregation_uses_serving_release_revision
)

echo "[2/7] コード・VLMプロファイル・コンセプト設定をバックアップしています: ${BACKUP_ROOT}"
mkdir -p "${BACKUP_ROOT}/files"
printf '%s\n' "${DEPLOY_FILES[@]}" > "${BACKUP_ROOT}/deploy-file-list.txt"
: > "${BACKUP_ROOT}/new-files.txt"
for relative_path in "${DEPLOY_FILES[@]}"; do
  target_path="${TARGET_ROOT}/${relative_path}"
  if [[ -f "${target_path}" ]]; then
    backup_path="${BACKUP_ROOT}/files/${relative_path}"
    mkdir -p "$(dirname "${backup_path}")"
    cp -a "${target_path}" "${backup_path}"
  else
    printf '%s\n' "${relative_path}" >> "${BACKUP_ROOT}/new-files.txt"
  fi
done
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

from app.rag.document_metadata_repository import document_metadata_repository
from app.rag.profile_repository import profile_repository

concept = document_metadata_repository.get_concept_settings()
payload = {
    'profiles': [profile_repository.get_profile(slot).model_dump(mode='json') for slot in (1, 2)],
    'concept': {
        'prompt_text': concept.prompt_text,
        'taxonomy_revision': concept.taxonomy_revision,
    },
}
Path(sys.argv[1]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8'
)
PY
)
chmod 0600 "${BACKUP_ROOT}/settings-before.json"

restore_code() {
  while IFS= read -r relative_path; do
    [[ -n "${relative_path}" ]] || continue
    install -D -m 0644 "${BACKUP_ROOT}/files/${relative_path}" "${TARGET_ROOT}/${relative_path}"
  done < <(find "${BACKUP_ROOT}/files" -type f -printf '%P\n')
  while IFS= read -r relative_path; do
    [[ -n "${relative_path}" ]] || continue
    target_path="${TARGET_ROOT}/${relative_path}"
    [[ "${target_path}" == "${TARGET_ROOT}/"* ]] && rm -f "${target_path}"
  done < "${BACKUP_ROOT}/new-files.txt"
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
}

restart_services() {
  fuser -k 8081/tcp >/dev/null 2>&1 || true
  fuser -k 5175/tcp >/dev/null 2>&1 || true
  bash "${TARGET_ROOT}/scripts/start-production-services.sh"
}

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  echo '反映に失敗したため、設定とコードを自動復元します' >&2
  restore_database_settings || true
  restore_code
  "${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app" || true
  restart_services || true
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo '[3/7] 生活イメージ抽出コードを反映しています'
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo '[4/7] VLMプロファイル1・2とAIコンセプト抽出プロンプトを更新しています'
DB_MUTATED=1
(
  cd "${TARGET_ROOT}/backend"
  .venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path('..') / '.env', override=False)
client_dir = os.getenv('ORACLE_CLIENT_LIB_DIR', '')
if not os.getenv('TNS_ADMIN') and client_dir:
    os.environ['TNS_ADMIN'] = str(Path(client_dir) / 'network' / 'admin')

from app.rag.concept_prompt_migration import apply_recommended_concept_prompt
from app.rag.profile_prompt_migration import apply_recommended_profiles

print(json.dumps({
    'profiles': apply_recommended_profiles(),
    'concept': apply_recommended_concept_prompt(),
}, ensure_ascii=False, default=str))
PY
)

echo '[5/7] 稼働先バックエンドの構文を検証しています'
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"

echo '[6/7] サービスを再起動しています'
restart_services

echo '[7/7] ヘルスチェックを実行しています'
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo '生活イメージ対応AIコンセプト抽出のデプロイが完了しました'
echo '新規アップロードは更新後の処理を使用します。既存文書の再処理Jobは開始していません。'
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-lifestyle-search-concepts.sh ${BACKUP_ROOT}"
