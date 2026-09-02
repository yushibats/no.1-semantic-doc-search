#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/search-concepts-document-sets}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  backend/app/rag/models.py
  backend/app/rag/oracle_repository.py
  backend/app/rag/search_api.py
  backend/app/rag/search_pipeline.py
  backend/app/rag/pipeline_api.py
  backend/app/rag/pipeline_config.py
  backend/app/rag/pipeline_engine.py
  backend/app/rag/pipeline_models.py
  backend/app/rag/pipeline_planner.py
  backend/app/rag/pipeline_repository.py
  backend/app/rag/document_metadata_api.py
  backend/app/rag/document_metadata_models.py
  backend/app/rag/document_metadata_repository.py
  backend/app/rag/document_metadata_schema.py
  frontend/app.js
  frontend/index.html
  frontend/src/modules/search.js
  frontend/src/modules/document-library.js
  frontend/src/modules/metadata-settings.js
  frontend/src/style.css
)

expected_target_hash() {
  case "$1" in
    backend/app/rag/models.py) printf '%s' '8c9a4ebd92370f482815f4dbb9d9c5ebe33f9c3f53510ed12e913c4587fc1178' ;;
    backend/app/rag/oracle_repository.py) printf '%s' '8e3d686b44c80b36a474fbb51afa0865da04411194ef28f083f3c59db1ff494e' ;;
    backend/app/rag/search_api.py) printf '%s' 'cdeb22c2c2e431947ba4fa78f58479652fa398d3625f5cbcf29614a2739a7dce' ;;
    backend/app/rag/search_pipeline.py) printf '%s' '5fbbbe431a5ae59f70ce669fb2c1b6d4bd0bf1d5405bad066426227fa7c90a46' ;;
    backend/app/rag/pipeline_api.py) printf '%s' '034f683af305759663b10337361344c400ac56117502bbee7b5bcfd33843945f' ;;
    backend/app/rag/pipeline_config.py) printf '%s' 'c292081704ebd011457d7aa3276e2c15eb43c63de6621685556b88e2ac460df7' ;;
    backend/app/rag/pipeline_engine.py) printf '%s' '0e1a1b518d658661f734a41af532540c96e6545db39369865042bac939ba2ed9' ;;
    backend/app/rag/pipeline_models.py) printf '%s' 'a5737b458cb5c83670e7679f8b280777da7cf1a17eb1df8c5e2421c3e30f0b7a' ;;
    backend/app/rag/pipeline_planner.py) printf '%s' 'ab536d47d2f67fff836e2f6317730115494ad19af6cbb6cc2eeaf40cecbf7c12' ;;
    backend/app/rag/pipeline_repository.py) printf '%s' '7f51ba9b145314c2ce23388e730641bf1d304a48a5ade02c82ea93f2a87875c3' ;;
    backend/app/rag/document_metadata_api.py) printf '%s' 'f2be087e0c81ef8b57b17e9cec396ff6a2bb5bade314b59a11d6b4b3644e2447' ;;
    backend/app/rag/document_metadata_models.py) printf '%s' 'a60e2997f9f3b7c05dc7aa422a4fa6c162e883c794d70a9bc35f1c8b04652d7a' ;;
    backend/app/rag/document_metadata_repository.py) printf '%s' '38d85b1d66876f3f954baa2c7c54f029d0422aec0fe91baa5a1d59e31af48104' ;;
    backend/app/rag/document_metadata_schema.py) printf '%s' '6ab4e7083650ed534c2bc6a72134522c5deee0b7a4bb7ad3072829878726d1be' ;;
    frontend/app.js) printf '%s' 'd573af8e550ff91ab7935fb5951377e0c2bc7067386d67c418ecd7cd1c6b8187' ;;
    frontend/index.html) printf '%s' '2f3b0afd63cfc9eca5639bd05e18f09a16487aafc94c7c7b38526b20aff463ca' ;;
    frontend/src/modules/search.js) printf '%s' '9107181116451a706ba69a2413e6faa60e46442372b174a3df291e8624d1ba8e' ;;
    frontend/src/modules/document-library.js) printf '%s' 'b2fbd11d038d6e20b6c283835ba3e945b4601adef45b862883fd3602c4707503' ;;
    frontend/src/modules/metadata-settings.js) printf '%s' '6071f6e64bcd65da920c699a65908fa4188c5da9ab36f8d9363fabcfa29468bb' ;;
    frontend/src/style.css) printf '%s' '8908b1dd9cb37eb48cff33446c1c35b88acbbe1bc57ff36c077b6468fe3542c5' ;;
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
  if [[ "${target_hash}" != "${source_hash}" && "${target_hash}" != "$(expected_target_hash "${relative_path}")" ]]; then
    echo "稼働先が確認時点から変更されているため、上書きせず中止します: ${relative_path}" >&2
    exit 1
  fi
done

echo "[1/7] 反映前テストを実行しています"
(
  cd "${SOURCE_ROOT}/backend"
  PYTHONPATH=. "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q tests/test_pipeline_contracts.py tests/test_document_metadata_feature.py tests/test_rag_profiles.py
)
(
  cd "${SOURCE_ROOT}/frontend"
  npm run test:ui
  npm run build
)

echo "[2/7] 変更対象をバックアップしています: ${BACKUP_ROOT}"
mkdir -p "${BACKUP_ROOT}/files"
printf '%s\n' "${DEPLOY_FILES[@]}" > "${BACKUP_ROOT}/deploy-file-list.txt"
: > "${BACKUP_ROOT}/new-files.txt"
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

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  echo "反映に失敗したため、コードを自動復元します" >&2
  restore_code
  "${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app" || true
  (cd "${TARGET_ROOT}/frontend" && npm run build) || true
  echo "追加DB表・列は後方互換のため保持します" >&2
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[3/7] 検索候補・案件グループ機能を反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[4/7] 追加スキーマを適用しています"
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

result = apply_document_library_schema()
if not result.get("ready"):
    raise SystemExit(json.dumps(result, ensure_ascii=False, default=str))
print(json.dumps(result, ensure_ascii=False, default=str))
'
)

echo "[5/7] 稼働先の構文と本番ビルドを検証しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
(cd "${TARGET_ROOT}/frontend" && npm run build)

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

echo "[6/7] バックエンドとフロントエンドを再起動しています"
stop_port 8081
stop_port 5175
bash "${TARGET_ROOT}/scripts/start-production-services.sh"

echo "[7/7] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "検索候補・案件グループ機能のデプロイが完了しました"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-search-concepts-and-document-sets.sh ${BACKUP_ROOT}"
