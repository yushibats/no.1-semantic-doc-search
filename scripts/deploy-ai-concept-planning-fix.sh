#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/ai-concept-planning-fix}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  backend/app/rag/pipeline_api.py
  backend/app/rag/document_metadata_api.py
)

expected_target_hash() {
  case "$1" in
    backend/app/rag/pipeline_api.py) printf '%s' '576beb714594209e78fa58a8cb292078978db5596fa86e5d36ed8cae9c7cf4e4' ;;
    backend/app/rag/document_metadata_api.py) printf '%s' '06c7338777cbfd1d8e8a104979b6cd09a83a293ea333d145bfb900f479dbb5d1' ;;
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
  if [[ ! -f "${source_path}" || ! -f "${target_path}" ]]; then
    echo "反映元または稼働先ファイルがありません: ${relative_path}" >&2
    exit 1
  fi
  source_hash="$(sha256sum "${source_path}" | awk '{print $1}')"
  target_hash="$(sha256sum "${target_path}" | awk '{print $1}')"
  expected_hash="$(expected_target_hash "${relative_path}")"
  if [[ "${target_hash}" != "${source_hash}" && "${target_hash}" != "${expected_hash}" ]]; then
    echo "稼働先が確認時点から変更されているため、上書きせず中止します: ${relative_path}" >&2
    exit 1
  fi
done

echo "[1/6] AI検索候補工程のJob計画テストを実行しています"
(
  cd "${SOURCE_ROOT}/backend"
  PYTHONPATH=. "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q \
    tests/test_pipeline_contracts.py \
    -k 'concept or disabled_concept'
)

echo "[2/6] 変更対象をバックアップしています: ${BACKUP_ROOT}"
mkdir -p "${BACKUP_ROOT}/files"
printf '%s\n' "${DEPLOY_FILES[@]}" > "${BACKUP_ROOT}/deploy-file-list.txt"
for relative_path in "${DEPLOY_FILES[@]}"; do
  backup_path="${BACKUP_ROOT}/files/${relative_path}"
  mkdir -p "$(dirname "${backup_path}")"
  cp -a "${TARGET_ROOT}/${relative_path}" "${backup_path}"
done

restore_code() {
  for relative_path in "${DEPLOY_FILES[@]}"; do
    install -D -m 0644 "${BACKUP_ROOT}/files/${relative_path}" "${TARGET_ROOT}/${relative_path}"
  done
}

restart_services() {
  fuser -k 8081/tcp >/dev/null 2>&1 || true
  fuser -k 5175/tcp >/dev/null 2>&1 || true
  bash "${TARGET_ROOT}/scripts/start-production-services.sh"
}

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  echo "反映に失敗したため、コードを自動復元します" >&2
  restore_code
  "${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app" || true
  restart_services || true
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[3/6] AI検索候補工程のJob計画修正を反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[4/6] 稼働先の構文を検証しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m py_compile \
  "${TARGET_ROOT}/backend/app/rag/pipeline_api.py" \
  "${TARGET_ROOT}/backend/app/rag/document_metadata_api.py"

echo "[5/6] バックエンドとフロントエンドを再起動しています"
restart_services

echo "[6/6] 設定ONと新規FULLジョブの計画を検証しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused \
  --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused \
  --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null
(
  cd "${TARGET_ROOT}/backend"
  .venv/bin/python - <<'PY'
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.document_metadata_repository import document_metadata_repository
from app.rag.pipeline_api import _plan
from app.rag.pipeline_models import PipelineJobRequest

settings = document_metadata_repository.get_concept_settings()
planned, _, _ = _plan(
    PipelineJobRequest(
        object_names=["documents/deployment-verification/source.jpg"],
        mode="FULL",
    )
)
components = [step.component_key for step in planned]
if not settings.enabled:
    raise SystemExit("AIコンセプト抽出の設定がOFFです")
if "concepts" not in components:
    raise SystemExit("FULLジョブにAI検索候補工程が含まれていません")
print({"concept_enabled": True, "concept_planned": True})
PY
)

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "AI検索候補工程のJob計画修正をデプロイしました"
echo "今後の通常アップロードは、設定ON時にAI検索候補抽出を実行します"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-ai-concept-planning-fix.sh ${BACKUP_ROOT}"
