#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/document-processing-details}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly EXPECTED_DOCUMENT_LIBRARY_SHA256="16f222b98192cbdf119234585fe314cf6c0d725277e14158237ae921a70076aa"
readonly EXPECTED_STYLE_SHA256="02e3b576e7923ab3d7cef991541ed6c8fc0099ffbf746318d27a3a79a519930a"
readonly EXPECTED_DOCUMENT_METADATA_API_SHA256="a6bdb900f6b0971ca916947507107a9bfa18e22b6a88972c9c2f314fe8333c23"
readonly EXPECTED_DOCUMENT_METADATA_MODELS_SHA256="b42f2a7bf5c6e926d01b8d3923a18a608815915bbf4048e7e979f10605d39b85"
readonly EXPECTED_DOCUMENT_METADATA_REPOSITORY_SHA256="49d908f8caf3ae61f01d162075c466483e2a1913760fced8a9ae1706e95d47a5"
readonly EXPECTED_PIPELINE_REPOSITORY_SHA256="f7de032e305a99d76cd4612619435d22f4ab34b8b3ea94dde62a7b6539096127"
readonly EXPECTED_PIPELINE_UI_SHA256="d55c761deb3a249d0878945bd0a676c80993bad66a53e59acd6bc8150ffe9569"
readonly EXPECTED_DOCUMENT_METADATA_SCHEMA_SHA256="5b0618dbb343222e14bb2632d85defdb04d254d97886dea1600adec13e755a6f"
readonly EXPECTED_DRAFT_CLASSIFIER_SHA256="ddb1a7b27d9d745f2dd0633e91c68802a750017d07d84e4b34d1fb76d467a6fd"
readonly DEPLOY_FILES=(
  backend/app/rag/document_metadata_api.py
  backend/app/rag/document_metadata_models.py
  backend/app/rag/document_metadata_repository.py
  backend/app/rag/document_metadata_schema.py
  backend/app/rag/draft_classifier.py
  backend/app/rag/pipeline_repository.py
  frontend/src/modules/document-library.js
  frontend/src/modules/pipeline.js
  frontend/src/style.css
)

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0" >&2
  exit 1
fi
if [[ ! -d "${SOURCE_ROOT}/.git" || ! -d "${TARGET_ROOT}/.git" ]]; then
  echo "SOURCE_ROOTまたはTARGET_ROOTがリポジトリではありません" >&2
  exit 1
fi

source_commit="$(git -C "${SOURCE_ROOT}" rev-parse HEAD)"
target_commit="$(git -c safe.directory="${TARGET_ROOT}" -C "${TARGET_ROOT}" rev-parse HEAD)"
if [[ "${source_commit}" != "${target_commit}" ]]; then
  echo "作業元と稼働先のGit基点が異なるため中止します" >&2
  exit 1
fi

expected_hash() {
  case "$1" in
    backend/app/rag/document_metadata_api.py) printf '%s' "${EXPECTED_DOCUMENT_METADATA_API_SHA256}" ;;
    backend/app/rag/document_metadata_models.py) printf '%s' "${EXPECTED_DOCUMENT_METADATA_MODELS_SHA256}" ;;
    backend/app/rag/document_metadata_repository.py) printf '%s' "${EXPECTED_DOCUMENT_METADATA_REPOSITORY_SHA256}" ;;
    backend/app/rag/document_metadata_schema.py) printf '%s' "${EXPECTED_DOCUMENT_METADATA_SCHEMA_SHA256}" ;;
    backend/app/rag/draft_classifier.py) printf '%s' "${EXPECTED_DRAFT_CLASSIFIER_SHA256}" ;;
    backend/app/rag/pipeline_repository.py) printf '%s' "${EXPECTED_PIPELINE_REPOSITORY_SHA256}" ;;
    frontend/src/modules/document-library.js) printf '%s' "${EXPECTED_DOCUMENT_LIBRARY_SHA256}" ;;
    frontend/src/modules/pipeline.js) printf '%s' "${EXPECTED_PIPELINE_UI_SHA256}" ;;
    frontend/src/style.css) printf '%s' "${EXPECTED_STYLE_SHA256}" ;;
    *) return 1 ;;
  esac
}

for relative_path in "${DEPLOY_FILES[@]}"; do
  source_path="${SOURCE_ROOT}/${relative_path}"
  target_path="${TARGET_ROOT}/${relative_path}"
  if [[ ! -f "${source_path}" || ! -f "${target_path}" ]]; then
    echo "反映元または稼働先ファイルがありません: ${relative_path}" >&2
    exit 1
  fi
  source_hash="$(sha256sum "${source_path}" | awk '{print $1}')"
  target_hash="$(sha256sum "${target_path}" | awk '{print $1}')"
  if [[ "${target_hash}" != "${source_hash}" && "${target_hash}" != "$(expected_hash "${relative_path}")" ]]; then
    echo "稼働先が確認時点から変更されているため、上書きせず中止します: ${relative_path}" >&2
    exit 1
  fi
done

mkdir -p "${BACKUP_ROOT}/files"
printf '%s\n' "${DEPLOY_FILES[@]}" > "${BACKUP_ROOT}/deploy-file-list.txt"
for relative_path in "${DEPLOY_FILES[@]}"; do
  backup_path="${BACKUP_ROOT}/files/${relative_path}"
  mkdir -p "$(dirname "${backup_path}")"
  cp -a "${TARGET_ROOT}/${relative_path}" "${backup_path}"
done

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  echo "反映に失敗したため、変更ファイルを自動復元します" >&2
  for relative_path in "${DEPLOY_FILES[@]}"; do
    install -D -m 0644 "${BACKUP_ROOT}/files/${relative_path}" "${TARGET_ROOT}/${relative_path}"
  done
  "${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app" || true
  (cd "${TARGET_ROOT}/frontend" && npm run build) || true
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[1/6] 文書管理UI・処理詳細API・写真タグ設定を反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[2/6] Python構文・バックエンドテスト・フロント本番ビルドを検証しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
(cd "${SOURCE_ROOT}/backend" && "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q tests/test_document_metadata_feature.py tests/test_pipeline_runtime.py)
(cd "${TARGET_ROOT}/frontend" && npm run build)

echo "[3/6] 文書種別の写真タグをDBへ追加しています"
(
  cd "${TARGET_ROOT}/backend"
  .venv/bin/python -c '
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.document_metadata_schema import apply_document_library_schema

print(json.dumps(apply_document_library_schema(), ensure_ascii=False, default=str))
'
)

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

echo "[4/6] バックエンドとフロントエンドを再起動しています"
stop_port 8081
stop_port 5175
bash "${TARGET_ROOT}/scripts/start-production-services.sh"

echo "[5/6] バックエンドのヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:8081/health >/dev/null

echo "[6/6] フロントエンドのヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:5175/ >/dev/null

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "処理詳細UIのデプロイが完了しました"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-document-processing-details.sh ${BACKUP_ROOT}"
