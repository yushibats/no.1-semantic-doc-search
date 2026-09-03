#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_ROOT="${APP_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/search-ready-vlm-prompts}"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly BACKUP_ROOT="${BACKUP_PARENT}/${TIMESTAMP}"
readonly SETTINGS_BACKUP="${BACKUP_ROOT}/settings-before.json"
DB_MUTATED=0

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0" >&2
  exit 1
fi
if [[ ! -x "${APP_ROOT}/backend/.venv/bin/python" || ! -f "${APP_ROOT}/.env" ]]; then
  echo "稼働先のアプリまたはバックエンド仮想環境が見つかりません: ${APP_ROOT}" >&2
  exit 1
fi

mkdir -p "${BACKUP_ROOT}"
chmod 0700 "${BACKUP_ROOT}"

restore_on_error() {
  local exit_code=$?
  trap - ERR
  if [[ "${DB_MUTATED}" -eq 1 && -f "${SETTINGS_BACKUP}" ]]; then
    echo "適用に失敗したため、3プロファイルとAI検索候補設定を復元します" >&2
    (
      cd "${APP_ROOT}/backend"
      .venv/bin/python - "${SETTINGS_BACKUP}" <<'PYRESTORE'
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.concept_prompt_migration import restore_concept_prompt
from app.rag.profile_prompt_migration import restore_search_ready_profiles

snapshot = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
restore_search_ready_profiles(snapshot["profiles"])
restore_concept_prompt(snapshot["concept"])
PYRESTORE
    ) || true
  fi
  exit "${exit_code}"
}
trap restore_on_error ERR

echo "[1/4] プロンプト実装と構文を検証しています"
"${APP_ROOT}/backend/.venv/bin/python" -m compileall -q "${APP_ROOT}/backend/app/rag"
(
  cd "${APP_ROOT}/backend"
  PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_search_ready_prompts.py
)

echo "[2/4] 現在の3プロファイルとAI検索候補設定をバックアップしています: ${BACKUP_ROOT}"
(
  cd "${APP_ROOT}/backend"
  .venv/bin/python - "${SETTINGS_BACKUP}" <<'PYSNAPSHOT'
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.concept_prompt_migration import snapshot_concept_prompt
from app.rag.profile_prompt_migration import snapshot_search_ready_profiles

payload = {
    "profiles": snapshot_search_ready_profiles(),
    "concept": snapshot_concept_prompt(),
}
Path(sys.argv[1]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
    encoding="utf-8",
)
PYSNAPSHOT
)
chmod 0600 "${SETTINGS_BACKUP}"

echo "[3/4] 3プロファイルとAI検索候補プロンプトを適用しています"
DB_MUTATED=1
(
  cd "${APP_ROOT}/backend"
  .venv/bin/python <<'PYAPPLY'
import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.concept_prompt_migration import apply_recommended_concept_prompt
from app.rag.profile_prompt_migration import apply_search_ready_profiles

print(json.dumps({"profiles": apply_search_ready_profiles()}, ensure_ascii=False, default=str))
print(json.dumps({"concept": apply_recommended_concept_prompt()}, ensure_ascii=False, default=str))
PYAPPLY
)

echo "[4/4] 適用結果を検証しています"
(
  cd "${APP_ROOT}/backend"
  .venv/bin/python <<'PYVERIFY'
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("..") / ".env", override=False)
client_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
if not os.getenv("TNS_ADMIN") and client_dir:
    os.environ["TNS_ADMIN"] = str(Path(client_dir) / "network" / "admin")

from app.rag.document_metadata_repository import document_metadata_repository
from app.rag.profile_prompts import SEARCH_CONCEPT_EXTRACTION_PROMPT
from app.rag.profile_prompt_migration import SEARCH_READY_PROFILE_UPDATES
from app.rag.profile_repository import profile_repository

for slot, (_, expected_prompt) in SEARCH_READY_PROFILE_UPDATES.items():
    actual = profile_repository.get_profile(slot)
    if actual.extraction_prompt != expected_prompt:
        raise RuntimeError(f"profile {slot} prompt verification failed")
settings = document_metadata_repository.get_concept_settings()
if settings.prompt_text != SEARCH_CONCEPT_EXTRACTION_PROMPT:
    raise RuntimeError("search concept prompt verification failed")
print("3プロファイルとAI検索候補プロンプトの一致を確認しました")
PYVERIFY
)

DB_MUTATED=0
trap - ERR
echo "検索強化VLMプロンプトの適用が完了しました"
echo "既存文書の再処理Jobは登録していません。新規登録・手動再実行から有効です。"
echo "バックアップ: ${BACKUP_ROOT}"
echo "ロールバック: sudo bash ${APP_ROOT}/scripts/rollback-search-ready-vlm-prompts.sh ${BACKUP_ROOT}"
