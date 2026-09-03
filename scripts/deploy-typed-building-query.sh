#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_ROOT="${APP_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly PREVIOUS_COMMIT="${PREVIOUS_COMMIT:-}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/typed-building-query}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly PROMPT_BACKUP_PARENT="${BACKUP_ROOT}/prompt-settings"
readonly DEPLOY_FILES=(
  backend/app/rag/case_classifier.py
  backend/app/rag/case_comparison_repository.py
  backend/app/rag/document_metadata_schema.py
  backend/app/rag/models.py
  backend/app/rag/oracle_repository.py
  backend/app/rag/query_condition_parser.py
  backend/app/rag/search_api.py
  backend/app/rag/search_pipeline.py
  frontend/index.html
  frontend/src/modules/search.js
  frontend/src/style.css
)

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0" >&2
  exit 1
fi
if [[ -z "${PREVIOUS_COMMIT}" ]]; then
  echo "更新前のGit commitをPREVIOUS_COMMITで指定してください" >&2
  exit 1
fi
if [[ ! -d "${APP_ROOT}/.git" || ! -x "${APP_ROOT}/backend/.venv/bin/python" ]]; then
  echo "稼働先のGitリポジトリまたはPython環境が見つかりません: ${APP_ROOT}" >&2
  exit 1
fi
if ! git -c safe.directory="${APP_ROOT}" -C "${APP_ROOT}" cat-file -e "${PREVIOUS_COMMIT}^{commit}"; then
  echo "更新前commitを参照できません: ${PREVIOUS_COMMIT}" >&2
  exit 1
fi

mkdir -p "${BACKUP_ROOT}/files"
chmod 0700 "${BACKUP_ROOT}"
printf '%s\n' "${PREVIOUS_COMMIT}" > "${BACKUP_ROOT}/previous-commit.txt"
git -c safe.directory="${APP_ROOT}" -C "${APP_ROOT}" rev-parse HEAD \
  > "${BACKUP_ROOT}/deployed-commit.txt"
: > "${BACKUP_ROOT}/restore-file-list.txt"
: > "${BACKUP_ROOT}/new-file-list.txt"

echo "[1/7] 更新前コードをバックアップしています: ${BACKUP_ROOT}"
for relative_path in "${DEPLOY_FILES[@]}"; do
  if [[ ! -f "${APP_ROOT}/${relative_path}" ]]; then
    echo "反映対象ファイルがありません: ${relative_path}" >&2
    exit 1
  fi
  if git -c safe.directory="${APP_ROOT}" -C "${APP_ROOT}" \
      cat-file -e "${PREVIOUS_COMMIT}:${relative_path}" 2>/dev/null; then
    backup_path="${BACKUP_ROOT}/files/${relative_path}"
    mkdir -p "$(dirname "${backup_path}")"
    git -c safe.directory="${APP_ROOT}" -C "${APP_ROOT}" \
      show "${PREVIOUS_COMMIT}:${relative_path}" > "${backup_path}"
    printf '%s\n' "${relative_path}" >> "${BACKUP_ROOT}/restore-file-list.txt"
  else
    printf '%s\n' "${relative_path}" >> "${BACKUP_ROOT}/new-file-list.txt"
  fi
done

restart_services() {
  fuser -k 8081/tcp >/dev/null 2>&1 || true
  fuser -k 5175/tcp >/dev/null 2>&1 || true
  bash "${APP_ROOT}/scripts/start-production-services.sh"
}

restore_code() {
  local relative_path
  while IFS= read -r relative_path; do
    [[ -n "${relative_path}" ]] || continue
    install -D -m 0644 \
      "${BACKUP_ROOT}/files/${relative_path}" \
      "${APP_ROOT}/${relative_path}"
  done < "${BACKUP_ROOT}/restore-file-list.txt"
  while IFS= read -r relative_path; do
    [[ -n "${relative_path}" ]] || continue
    rm -f -- "${APP_ROOT}/${relative_path}"
  done < "${BACKUP_ROOT}/new-file-list.txt"
}

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  echo "反映に失敗したためコードを自動復元します" >&2
  restore_code
  "${APP_ROOT}/backend/.venv/bin/python" -m compileall -q \
    "${APP_ROOT}/backend/app" || true
  (cd "${APP_ROOT}/frontend" && npm run build) || true
  restart_services || true
  echo "自動復元先: ${BACKUP_ROOT}" >&2
  echo "追加済みDBスキーマは後方互換のため残します（既存データは削除しません）" >&2
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[2/7] 前回改修の回帰テストを実行しています"
(
  cd "${APP_ROOT}/backend"
  PYTHONPATH=. .venv/bin/python -m pytest -q \
    tests/test_query_condition_parser.py \
    tests/test_case_classifier.py
  PYTHONPATH=. .venv/bin/python -m pytest -q \
    tests/test_document_metadata_feature.py \
    -k 'building_conditions or typed_metadata_search'
)

echo "[3/7] バックエンドの構文とフロントエンドを検証しています"
"${APP_ROOT}/backend/.venv/bin/python" -m compileall -q \
  "${APP_ROOT}/backend/app"
(
  cd "${APP_ROOT}/frontend"
  npm run build
  npm run test:ui
)

echo "[4/7] 面積を含む型付き属性スキーマを適用しています"
(
  cd "${APP_ROOT}/backend"
  .venv/bin/python <<'PYSCHEMA'
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
PYSCHEMA
)

echo "[5/7] バックエンドとフロントエンドを再起動しています"
restart_services

echo "[6/7] サービスのヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 60 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 60 --retry-delay 1 \
  --retry-connrefused --connect-timeout 2 \
  http://127.0.0.1:5175/ai/ >/dev/null

echo "[7/7] 3種類のVLMとAI検索候補プロンプトを適用しています"
env BACKUP_PARENT="${PROMPT_BACKUP_PARENT}" APP_ROOT="${APP_ROOT}" \
  bash "${APP_ROOT}/scripts/apply-search-ready-vlm-prompts.sh"

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "自然言語条件解析・型付き面積属性・検索フィルタをデプロイしました"
echo "既存文書の再処理Jobは登録していません"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${APP_ROOT}/scripts/rollback-typed-building-query.sh ${BACKUP_ROOT}"
