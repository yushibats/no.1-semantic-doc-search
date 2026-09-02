#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/search-empty-selection-fix}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  frontend/src/style.css
)

expected_target_hash() {
  case "$1" in
    frontend/src/style.css) printf '%s' 'c4ed859e59259ff51556141a839f171028d5790cbeaabe0b870b43ee1c44e07a' ;;
    *) return 1 ;;
  esac
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0" >&2
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

echo "[1/5] 検索画面のUI回帰テストを実行しています"
(cd "${SOURCE_ROOT}/frontend" && npm run test:ui)

echo "[2/5] 変更対象をバックアップしています: ${BACKUP_ROOT}"
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
  echo "反映に失敗したため画面コードを自動復元します" >&2
  restore_code
  (cd "${TARGET_ROOT}/frontend" && npm run build) || true
  restart_services || true
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[3/5] 空の選択タグ枠を非表示にして本番ビルドしています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done
(cd "${TARGET_ROOT}/frontend" && npm run build)

echo "[4/5] サービスを再起動しています"
restart_services

echo "[5/5] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "検索画面の空枠修正をデプロイしました"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-search-empty-selection-fix.sh ${BACKUP_ROOT}"
