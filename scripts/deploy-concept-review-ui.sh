#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/concept-review-ui}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  frontend/index.html
  frontend/src/modules/metadata-settings.js
  frontend/src/modules/search.js
  frontend/src/style.css
)

expected_target_hash() {
  case "$1" in
    frontend/index.html) printf "%s" "a8f62a1adb8350d35bc86cf9d6f4c74571e2f61b802eae606aac2fd0af1b4f24" ;;
    frontend/src/modules/metadata-settings.js) printf "%s" "e5c856498c68cc05cfd085695e04cdeb5fc56c5d3953599c5ab9710bdba4f552" ;;
    frontend/src/modules/search.js) printf "%s" "6f500a2d810f0ee78a458ef1fe637d1d9a98d76cce2af219ac00f8d2010854f5" ;;
    frontend/src/style.css) printf "%s" "f344ad0059b70666faeb5f79d54ee97866162de6f7eb5b91dc1386fe5abd2b1a" ;;
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
  if [[ "${target_hash}" != "${source_hash}" && "${target_hash}" != "$(expected_target_hash "${relative_path}")" ]]; then
    echo "稼働先が確認時点から変更されているため、上書きせず中止します: ${relative_path}" >&2
    exit 1
  fi
done

echo "[1/6] 検索候補承認UIの回帰テストを実行しています"
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

restart_frontend() {
  fuser -k 5175/tcp >/dev/null 2>&1 || true
  (
    cd "${TARGET_ROOT}/frontend"
    nohup npm run preview -- --host 0.0.0.0 --port 5175 \
      > /var/log/app-frontend.log 2>&1 &
  )
}

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  echo "反映に失敗したため、変更ファイルを自動復元します" >&2
  restore_code
  (cd "${TARGET_ROOT}/frontend" && npm run build) || true
  restart_frontend || true
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[3/6] 折りたたみ・一括承認・スクロール保持UIを反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[4/6] 稼働先の本番フロントエンドをビルドしています"
(cd "${TARGET_ROOT}/frontend" && npm run build)

echo "[5/6] フロントエンドを再起動しています"
fuser -k 5175/tcp >/dev/null 2>&1 || true
(
  cd "${TARGET_ROOT}/frontend"
  nohup npm run preview -- --host 0.0.0.0 --port 5175 \
    > /var/log/app-frontend.log 2>&1 &
)

echo "[6/6] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "検索候補承認UIのデプロイが完了しました"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-concept-review-ui.sh ${BACKUP_ROOT}"
