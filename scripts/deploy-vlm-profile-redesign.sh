#!/usr/bin/env bash
set -Eeuo pipefail

readonly SOURCE_ROOT="${SOURCE_ROOT:-/home/ubuntu/projects/my-project/no.1-semantic-doc-search}"
readonly TARGET_ROOT="${TARGET_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/vlm-profile-redesign}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly DEPLOY_FILES=(
  backend/app/rag/models.py
  backend/app/rag/index_pipeline.py
  backend/app/rag/pipeline_engine.py
  backend/app/rag/settings_api.py
  backend/app/rag/vlm_prompting.py
  backend/app/rag/profile_prompts.py
  backend/app/rag/profile_prompt_migration.py
  frontend/src/modules/retrieval-settings.js
  frontend/src/style.css
)

expected_target_hash() {
  case "$1" in
    backend/app/rag/models.py) printf '%s' '71f17d8828dabb0a5a09ab1af7e313228f7ec806e731b3041d30ef24da56813a' ;;
    backend/app/rag/index_pipeline.py) printf '%s' '90099e0eed266d67b2deb0d089d932a46f21f1c25fec423ac61644d77d299bc9' ;;
    backend/app/rag/pipeline_engine.py) printf '%s' 'e53e0124ecfd76b644ec6d16e3dfd1644a1feabec12b6b869efe410e2808c1d3' ;;
    backend/app/rag/settings_api.py) printf '%s' '75df36bfb75e6d5e4a79bd41ce82fa6fe096140865771ff28798e7b6f4698122' ;;
    frontend/src/modules/retrieval-settings.js) printf '%s' '90715aa3bd1d2f0da60627c6d68c810bfbd3b7ab6be553909c3bd93152abe13a' ;;
    frontend/src/style.css) printf '%s' '2c2e9cf2a3c7b8ec2b0a1051afdcfeccc33d422059467cbc9b7a60127dd2a717' ;;
    backend/app/rag/vlm_prompting.py|backend/app/rag/profile_prompts.py|backend/app/rag/profile_prompt_migration.py) printf '%s' 'ABSENT' ;;
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
  expected_hash="$(expected_target_hash "${relative_path}")"
  if [[ ! -f "${source_path}" ]]; then
    echo "反映元ファイルがありません: ${relative_path}" >&2
    exit 1
  fi
  if [[ -f "${target_path}" ]]; then
    source_hash="$(sha256sum "${source_path}" | awk '{print $1}')"
    target_hash="$(sha256sum "${target_path}" | awk '{print $1}')"
    if [[ "${target_hash}" != "${source_hash}" && "${target_hash}" != "${expected_hash}" ]]; then
      echo "稼働先が確認時点から変更されているため、上書きせず中止します: ${relative_path}" >&2
      exit 1
    fi
  elif [[ "${expected_hash}" != "ABSENT" ]]; then
    echo "稼働先ファイルが見つからないため中止します: ${relative_path}" >&2
    exit 1
  fi
done

echo "[1/7] VLMプロファイル変更の反映前テストを実行しています"
(
  cd "${SOURCE_ROOT}/backend"
  PYTHONPATH=. "${TARGET_ROOT}/backend/.venv/bin/python" -m pytest -q tests/test_rag_profiles.py tests/test_pipeline_runtime.py
)
(
  cd "${SOURCE_ROOT}/frontend"
  npm run test:ui
  npm run build
)

echo "[2/7] コードと現在のプロファイルRevisionをバックアップしています: ${BACKUP_ROOT}"
mkdir -p "${BACKUP_ROOT}/files"
printf '%s\n' "${DEPLOY_FILES[@]}" > "${BACKUP_ROOT}/deploy-file-list.txt"
: > "${BACKUP_ROOT}/new-files.txt"
for relative_path in "${DEPLOY_FILES[@]}"; do
  target_path="${TARGET_ROOT}/${relative_path}"
  if [[ -f "${target_path}" ]]; then
    backup_path="${BACKUP_ROOT}/files/${relative_path}"
    mkdir -p "$(dirname "${backup_path}")"
    cp -a "${target_path}" "${backup_path}"
  else
    printf '%s\n' "${relative_path}" >> "${BACKUP_ROOT}/new-files.txt"
  fi
done
(
  cd "${TARGET_ROOT}/backend"
  .venv/bin/python - "${BACKUP_ROOT}/profiles-before.json" <<'PY'
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.profile_repository import profile_repository

snapshot = [profile_repository.get_profile(slot).model_dump(mode="json") for slot in (1, 2)]
Path(sys.argv[1]).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
PY
)
chmod 0600 "${BACKUP_ROOT}/profiles-before.json"

restore_code() {
  while IFS= read -r relative_path; do
    [[ -n "${relative_path}" ]] || continue
    install -D -m 0644 "${BACKUP_ROOT}/files/${relative_path}" "${TARGET_ROOT}/${relative_path}"
  done < <(find "${BACKUP_ROOT}/files" -type f -printf '%P\n')
  while IFS= read -r relative_path; do
    [[ -n "${relative_path}" ]] || continue
    target_path="${TARGET_ROOT}/${relative_path}"
    if [[ "${target_path}" == "${TARGET_ROOT}/"* ]]; then
      rm -f "${target_path}"
    fi
  done < "${BACKUP_ROOT}/new-files.txt"
}

restore_profiles() {
  [[ -f "${BACKUP_ROOT}/profiles-before.json" ]] || return 0
  (
    cd "${TARGET_ROOT}/backend"
    .venv/bin/python - "${BACKUP_ROOT}/profiles-before.json" <<'PY'
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.profile_prompt_migration import restore_profiles

snapshot = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(json.dumps(restore_profiles(snapshot), ensure_ascii=False, default=str))
PY
  )
}

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  echo "反映に失敗したため、プロファイルとコードを自動復元します" >&2
  restore_profiles || true
  restore_code
  "${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app" || true
  (cd "${TARGET_ROOT}/frontend" && npm run build) || true
  fuser -k 8081/tcp >/dev/null 2>&1 || true
  fuser -k 5175/tcp >/dev/null 2>&1 || true
  bash "${TARGET_ROOT}/scripts/start-production-services.sh" || true
  exit "${exit_code}"
}
trap rollback_on_error ERR

echo "[3/7] VLM入力・帖数計算・設定画面を反映しています"
for relative_path in "${DEPLOY_FILES[@]}"; do
  install -D -m 0644 "${SOURCE_ROOT}/${relative_path}" "${TARGET_ROOT}/${relative_path}"
done

echo "[4/7] プロファイル1・2を更新しています（再処理Jobは開始しません）"
(
  cd "${TARGET_ROOT}/backend"
  .venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.profile_prompt_migration import apply_recommended_profiles

print(json.dumps(apply_recommended_profiles(), ensure_ascii=False, default=str))
PY
)

echo "[5/7] 稼働先の構文と本番ビルドを検証しています"
"${TARGET_ROOT}/backend/.venv/bin/python" -m compileall -q "${TARGET_ROOT}/backend/app"
(cd "${TARGET_ROOT}/frontend" && npm run build)

echo "[6/7] バックエンドとフロントエンドを再起動しています"
fuser -k 8081/tcp >/dev/null 2>&1 || true
fuser -k 5175/tcp >/dev/null 2>&1 || true
bash "${TARGET_ROOT}/scripts/start-production-services.sh"

echo "[7/7] ヘルスチェックを実行しています"
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:8081/health >/dev/null
curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused --connect-timeout 2 http://127.0.0.1:5175/ >/dev/null

trap - ERR
touch "${BACKUP_ROOT}/deployment-complete"
echo "VLMプロファイル再設計のデプロイが完了しました"
echo "既存文書のVLM再処理は開始していません。設定画面から明示的に実行してください。"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${SOURCE_ROOT}/scripts/rollback-vlm-profile-redesign.sh ${BACKUP_ROOT}"
