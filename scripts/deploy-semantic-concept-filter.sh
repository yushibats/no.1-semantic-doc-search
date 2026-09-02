#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/semantic-concept-filter}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  backend/app/rag/search_api.py
  frontend/index.html
  frontend/src/modules/search.js
  frontend/src/style.css
)

expected_target_hash() {
  case "$1" in
    backend/app/rag/search_api.py) printf '%s' 'c6c533a04a6d37ad845b0bce08c8726c3e29da60ef5a48d91fc42f792b107db0' ;;
    frontend/index.html) printf '%s' '974ee1c77437addcdec7f292cc9593dcd39ca44acda3fe78f3d5864b61605eb5' ;;
    frontend/src/modules/search.js) printf '%s' 'a7601e175e0b027eba6bf3df79314d7aa5cd5dbf81d7599955da847d897877e0' ;;
    frontend/src/style.css) printf '%s' 'a02d25f4e45fea23c36ece23e04c164adb7573c072da6af59978645585c5ca88' ;;
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

echo "[1/6] 意味検索候補の回帰テストを実行しています"
(
  cd "${SOURCE_ROOT}/backend"
  PYTHONPATH=. "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q     tests/test_rag_profiles.py     -k 'search_concept_suggestions_rank_sentences_and_reject_unknown_ids'
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

echo "[3/6] 入力文による検索条件の意味絞り込みを反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[4/6] 稼働先の構文と本番ビルドを検証しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
(cd "${TARGET_ROOT}/frontend" && npm run build)

echo "[5/6] バックエンドとフロントエンドを再起動しています"
restart_services

echo "[6/6] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused   --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused   --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "検索条件の意味絞り込みをデプロイしました"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-semantic-concept-filter.sh ${BACKUP_ROOT}"
