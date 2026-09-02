#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/document-library}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly RESUME_BACKUP_ROOT="${RESUME_BACKUP_ROOT:-}"
readonly BACKUP_ROOT="${RESUME_BACKUP_ROOT:-${BACKUP_PARENT}/${TIMESTAMP}}"

readonly DEPLOY_FILES=(
  backend/app/main.py
  backend/app/rag/models.py
  backend/app/rag/oracle_repository.py
  backend/app/rag/oracle_schema.py
  backend/app/rag/pipeline_repository.py
  backend/app/rag/search_api.py
  backend/app/rag/search_pipeline.py
  backend/app/rag/classification_rules.py
  backend/app/rag/document_metadata_api.py
  backend/app/rag/document_metadata_models.py
  backend/app/rag/document_metadata_repository.py
  backend/app/rag/document_metadata_schema.py
  backend/app/rag/draft_classifier.py
  frontend/app.js
  frontend/index.html
  frontend/src/modules/search.js
  frontend/src/style.css
  frontend/src/modules/document-library.js
  frontend/src/modules/metadata-settings.js
  scripts/start-production-services.sh
)

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
if ! git -c safe.directory="${TARGET_ROOT}" -C "${TARGET_ROOT}" \
  diff --quiet -- "${DEPLOY_FILES[@]}"; then
  unexpected_changes=()
  for relative_path in "${DEPLOY_FILES[@]}"; do
    if ! cmp -s "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"; then
      unexpected_changes+=("${relative_path}")
    fi
  done
  if (( ${#unexpected_changes[@]} > 0 )); then
    if [[ -z "${RESUME_BACKUP_ROOT}" ]]; then
      echo "稼働先の反映対象ファイルに、作業元と一致しない未退避の変更があります" >&2
      printf '  %s\n' "${unexpected_changes[@]}" >&2
      exit 1
    fi
    echo "再開時の修正版を反映します:"
    printf '  %s\n' "${unexpected_changes[@]}"
  else
    echo "前回の途中反映と作業元が一致したため、安全に再開します"
  fi
fi

if [[ -n "${RESUME_BACKUP_ROOT}" ]]; then
  if [[ "${BACKUP_ROOT}" != "${BACKUP_PARENT}/"* ]] || \
    [[ ! -d "${BACKUP_ROOT}/files" ]] || \
    [[ ! -f "${BACKUP_ROOT}/new-files.txt" ]] || \
    [[ ! -f "${BACKUP_ROOT}/deploy-file-list.txt" ]]; then
    echo "再開用バックアップが正しくありません: ${BACKUP_ROOT}" >&2
    exit 1
  fi
  if [[ -e "${BACKUP_ROOT}/deployment-complete" ]]; then
    echo "指定されたバックアップは完了済みデプロイのものです" >&2
    exit 1
  fi
  for relative_path in "${DEPLOY_FILES[@]}"; do
    if grep -Fqx -- "${relative_path}" "${BACKUP_ROOT}/deploy-file-list.txt"; then
      continue
    fi
    if [[ -e "${TARGET_ROOT}/${relative_path}" ]]; then
      echo "元の状態を判定できない追加ファイルがあるため再開できません: ${relative_path}" >&2
      exit 1
    fi
    printf '%s\n' "${relative_path}" >> "${BACKUP_ROOT}/deploy-file-list.txt"
    printf '%s\n' "${relative_path}" >> "${BACKUP_ROOT}/new-files.txt"
  done
  echo "[1/6] 元のバックアップを維持して再開します: ${BACKUP_ROOT}"
else
  mkdir -p "${BACKUP_ROOT}/files"
  : > "${BACKUP_ROOT}/new-files.txt"
  printf '%s\n' "${DEPLOY_FILES[@]}" > "${BACKUP_ROOT}/deploy-file-list.txt"

  echo "[1/6] 変更対象をバックアップしています: ${BACKUP_ROOT}"
  for relative_path in "${DEPLOY_FILES[@]}"; do
    source_path="${SOURCE_ROOT}/${relative_path}"
    target_path="${TARGET_ROOT}/${relative_path}"
    if [[ ! -f "${source_path}" ]]; then
      echo "反映元ファイルがありません: ${source_path}" >&2
      exit 1
    fi
    if [[ -f "${target_path}" ]]; then
      backup_path="${BACKUP_ROOT}/files/${relative_path}"
      mkdir -p "$(dirname "${backup_path}")"
      cp -a "${target_path}" "${backup_path}"
    else
      printf '%s\n' "${relative_path}" >> "${BACKUP_ROOT}/new-files.txt"
    fi
  done
fi

echo "[2/6] 限定ファイルを反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  target_path="${TARGET_ROOT}/${relative_path}"
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${target_path}"
done

echo "[3/6] Python構文とフロント本番ビルドを検証しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
(
  cd "${TARGET_ROOT}/frontend"
  npm run build
)

echo "[4/6] 文書ライブラリの追加スキーマを適用しています"
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

stop_processes() {
  local pattern="$1"
  local pids
  pids="$(pgrep -f "${pattern}" || true)"
  if [[ -z "${pids}" ]]; then
    return
  fi
  kill ${pids}
  for _ in {1..20}; do
    if ! pgrep -f "${pattern}" >/dev/null; then
      return
    fi
    sleep 0.25
  done
  pids="$(pgrep -f "${pattern}" || true)"
  if [[ -n "${pids}" ]]; then
    kill -9 ${pids}
  fi
}

echo "[5/6] バックエンドとフロントエンドだけを再起動しています"
stop_processes '/u01/aipoc/no.1-semantic-doc-search/backend/.venv/bin/python.*uvicorn app.main:app'
stop_processes 'uv run --directory backend uvicorn app.main:app --host'
stop_processes '/u01/aipoc/no.1-semantic-doc-search/frontend/node_modules/.bin/vite preview'
bash "${TARGET_ROOT}/scripts/start-production-services.sh"

echo "[6/6] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:5175/ >/dev/null

touch "${BACKUP_ROOT}/deployment-complete"
echo "デプロイが完了しました"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-document-library-feature.sh ${BACKUP_ROOT}"
