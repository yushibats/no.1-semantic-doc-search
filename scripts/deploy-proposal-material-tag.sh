#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/proposal-material-tag}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  backend/app/rag/document_metadata_schema.py
  backend/app/rag/draft_classifier.py
)

expected_target_hash() {
  case "$1" in
    backend/app/rag/document_metadata_schema.py)
      printf '%s' '49150ff5664c726120fce7bbae4914f93fc5cc0b673883ab6272c1d4a7619f34'
      ;;
    backend/app/rag/draft_classifier.py)
      printf '%s' 'd0253e3d7b71ab27f93c600605740db0bdc408e378c468d7157f0228c286c652'
      ;;
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

echo "[1/6] 提案資料タグの反映前テストを実行しています"
(
  cd "${SOURCE_ROOT}/backend"
  PYTHONPATH=. "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q tests/test_document_metadata_feature.py
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

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  echo "反映に失敗したため、変更ファイルを自動復元します" >&2
  restore_code
  "${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app" || true
  fuser -k 8081/tcp >/dev/null 2>&1 || true
  fuser -k 5175/tcp >/dev/null 2>&1 || true
  bash "${TARGET_ROOT}/scripts/start-production-services.sh" || true
  echo "追加したDBタグ・ルールリビジョンは後方互換のため保持します" >&2
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[3/6] 提案資料タグと分類ガイダンスを反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[4/6] 追加タグと既定ルールをDBへ適用しています"
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

echo "[5/6] 構文を検証してサービスを再起動しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
fuser -k 8081/tcp >/dev/null 2>&1 || true
fuser -k 5175/tcp >/dev/null 2>&1 || true
bash "${TARGET_ROOT}/scripts/start-production-services.sh"

echo "[6/6] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "文書種別「提案資料」のデプロイが完了しました"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-proposal-material-tag.sh ${BACKUP_ROOT}"
