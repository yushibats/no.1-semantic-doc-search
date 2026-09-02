#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/document-bulk-search-details}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  backend/app/rag/document_metadata_models.py
  backend/app/rag/document_metadata_repository.py
  backend/app/rag/document_metadata_api.py
  backend/app/rag/search_api.py
  frontend/src/modules/document-library.js
  frontend/src/modules/search.js
  frontend/src/modules/utils.js
  frontend/src/style.css
)

expected_target_hash() {
  case "$1" in
    backend/app/rag/document_metadata_models.py) printf '%s' 'c028637d7e2311a7f8046e2c29a584ec2aeca49d288a4baeeb83782421a075c4' ;;
    backend/app/rag/document_metadata_repository.py) printf '%s' '6833ac947863cefa5a5042d83d4daa046dd44ca109be8d27ce6bdfa310a312cf' ;;
    backend/app/rag/document_metadata_api.py) printf '%s' 'a83d6bded0e7d45a90c7ebe3ac92bbe7155d789974595133697927bcb3fbc63f' ;;
    backend/app/rag/search_api.py) printf '%s' '147d858c28e07e979e766ecf8a20f8c3ea379b3e4109b34bbb32a384917c0dc6' ;;
    frontend/src/modules/document-library.js) printf '%s' '247f46e818c5de9290479795b0dff06de6339f6cf69dd7be468599862773bb00' ;;
    frontend/src/modules/search.js) printf '%s' 'e0d9f847ecef1260289de95011428e504023c7334c4a12807e3e8b04558e9c9f' ;;
    frontend/src/modules/utils.js) printf '%s' '78261205cfa1c5b6c278e41c3cd720e47fd4de102c20bfb6d1a276d622dbf9a3' ;;
    frontend/src/style.css) printf '%s' 'a49a98dcdc3f60c633228d832b4e7458818a352d9b46806dc9d9ecf85d55b36e' ;;
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
if [[ ! -d "${TARGET_ROOT}/frontend/node_modules" ]]; then
  echo "稼働先のfrontend依存関係が見つかりません" >&2
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

echo "[1/6] 文書一括操作と検索詳細の回帰テストを実行しています"
(
  cd "${SOURCE_ROOT}/backend"
  PYTHONPATH=. "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q \
    tests/test_document_metadata_feature.py tests/test_rag_profiles.py \
    -k 'bulk_document_selection or document_action_queries or artifact_cleanup or registered_document_delete or search_evidence_ai_explanation'
)
(
  cd "${SOURCE_ROOT}/frontend"
  npm run test:ui
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
  echo "反映に失敗したためコードを自動復元します" >&2
  restore_code
  "${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app" || true
  (cd "${TARGET_ROOT}/frontend" && npm run build) || true
  restart_services || true
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[3/6] 文書一括操作と検索ページ詳細を反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[4/6] 稼働先の構文と本番ビルドを検証しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
(cd "${TARGET_ROOT}/frontend" && npm run build)

echo "[5/6] バックエンドとフロントエンドを再起動しています"
restart_services

echo "[6/6] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused \
  --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused \
  --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "文書一括操作・検索ページ詳細のデプロイが完了しました"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-document-bulk-search-details.sh ${BACKUP_ROOT}"
