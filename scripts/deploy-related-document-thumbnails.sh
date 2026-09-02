#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/related-document-thumbnails}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  backend/app/rag/models.py
  backend/app/rag/oracle_repository.py
  backend/app/rag/search_pipeline.py
  frontend/src/modules/search.js
  frontend/src/style.css
)

expected_target_hash() {
  case "$1" in
    backend/app/rag/models.py) printf '%s' 'aa9f4bd04e50f68eb742398db4c27c43fe2f1cc1591365067440fcc8c23104f3' ;;
    backend/app/rag/oracle_repository.py) printf '%s' 'e8d0f443d1dbbf7c1ffb90b5b70e1c1d01109689edd52fdf213dc9bbb8ab1853' ;;
    backend/app/rag/search_pipeline.py) printf '%s' '3ddd302bcdc2fdbd223a6e42e7ff90d8c04db8b0699767c701cd583b321e14a5' ;;
    frontend/src/modules/search.js) printf '%s' '2cfcfa2bf699b7bdc81fafdc59eb75e6b3cd375a91c11f6fa8e05c68cc0aec9a' ;;
    frontend/src/style.css) printf '%s' '8e9bf564682cdd7e16c17674468c681c07f611430b77bd5d59027dc441ba91ea' ;;
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

echo "[1/6] 関連資料サムネイルの回帰テストを実行しています"
(
  cd "${SOURCE_ROOT}/backend"
  PYTHONPATH=. "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q \
    tests/test_document_metadata_feature.py \
    -k companion_documents_select_the_first_published_page_thumbnail
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

echo "[3/6] 関連資料の小型サムネイルと拡大表示を反映しています"
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
echo "関連資料サムネイルのデプロイが完了しました"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-related-document-thumbnails.sh ${BACKUP_ROOT}"
