#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/processing-job-history-fix}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  backend/app/rag/pipeline_engine.py
  backend/app/rag/pipeline_repository.py
  backend/app/rag/document_metadata_api.py
  frontend/src/modules/document-library.js
  frontend/src/style.css
)

expected_target_hash() {
  case "$1" in
    backend/app/rag/pipeline_engine.py) printf '%s' '87c9d42b53b281dff1b901bc1cbf967c03b9abcd4dda83d5a4288520f1534b00' ;;
    backend/app/rag/pipeline_repository.py) printf '%s' 'fdc81538cc18e03941c7168da74c40fe338b99285076590bd323c7e19c613f81' ;;
    backend/app/rag/document_metadata_api.py) printf '%s' 'a83d6bded0e7d45a90c7ebe3ac92bbe7155d789974595133697927bcb3fbc63f' ;;
    frontend/src/modules/document-library.js) printf '%s' '247f46e818c5de9290479795b0dff06de6339f6cf69dd7be468599862773bb00' ;;
    frontend/src/style.css) printf '%s' 'f344ad0059b70666faeb5f79d54ee97866162de6f7eb5b91dc1386fe5abd2b1a' ;;
    *) return 1 ;;
  esac
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0" >&2
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
    echo "稼働先が確認時点から変更されているため中止します: ${relative_path}" >&2
    exit 1
  fi
done

echo "[1/7] 追加Jobと処理履歴の回帰テストを実行しています"
(
  cd "${SOURCE_ROOT}/backend"
  PYTHONPATH=. "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q \
    tests/test_pipeline_runtime.py \
    -k 'concept_only or release_for_enrichment or repair_stranded or job_lineage'
)
(
  cd "${SOURCE_ROOT}/frontend"
  npm run test:ui
)

echo "[2/7] 変更対象をバックアップしています: ${BACKUP_ROOT}"
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
  echo "反映に失敗したためコードを自動復元します" >&2
  restore_code
  "${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app" || true
  (cd "${TARGET_ROOT}/frontend" && npm run build) || true
  restart_services || true
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[3/7] 追加Jobの非破壊実行と処理履歴タブを反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[4/7] 稼働先の構文と本番ビルドを検証しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
(cd "${TARGET_ROOT}/frontend" && npm run build)

echo "[5/7] 旧不具合で処理中に残った文書を安全条件付きで復旧しています"
(
  cd "${TARGET_ROOT}/backend"
  .venv/bin/python - "${BACKUP_ROOT}/repair-result.json" <<'PY'
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

repaired = pipeline_repository.repair_stranded_concept_only_drafts()
result = {"repaired_count": len(repaired), "object_names": repaired}
Path(sys.argv[1]).write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result, ensure_ascii=False))
PY
)

echo "[6/7] バックエンドとフロントエンドを再起動しています"
restart_services

echo "[7/7] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused \
  --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused \
  --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "追加Job・処理履歴タブ修正のデプロイが完了しました"
echo "復旧結果: ${BACKUP_ROOT}/repair-result.json"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-processing-job-history-fix.sh ${BACKUP_ROOT}"
