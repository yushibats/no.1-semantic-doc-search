#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_ROOT="${APP_ROOT:-/u01/aipoc/no.1-semantic-doc-search}"
readonly BACKUP_PARENT="${BACKUP_PARENT:-/u01/aipoc/backups/search-ready-vlm-prompts}"
readonly BACKUP_ROOT="${1:-}"
readonly SETTINGS_BACKUP="${BACKUP_ROOT}/settings-before.json"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root権限で実行してください: sudo bash $0 <backup-directory>" >&2
  exit 1
fi
case "${BACKUP_ROOT}" in
  "${BACKUP_PARENT}/"*) ;;
  *) echo "想定外のバックアップパスです: ${BACKUP_ROOT}" >&2; exit 1 ;;
esac
if [[ ! -f "${SETTINGS_BACKUP}" || ! -x "${APP_ROOT}/backend/.venv/bin/python" ]]; then
  echo "バックアップまたは稼働先仮想環境が見つかりません" >&2
  exit 1
fi

echo "3プロファイルとAI検索候補設定を以前のリビジョンへ戻しています"
(
  cd "${APP_ROOT}/backend"
  .venv/bin/python - "${SETTINGS_BACKUP}" <<'PYROLLBACK'
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
print(json.dumps(restore_search_ready_profiles(snapshot["profiles"]), ensure_ascii=False, default=str))
print(json.dumps(restore_concept_prompt(snapshot["concept"]), ensure_ascii=False, default=str))
PYROLLBACK
)

echo "検索強化VLMプロンプトのロールバックが完了しました"
echo "既存文書の処理Jobには変更を加えていません。"
