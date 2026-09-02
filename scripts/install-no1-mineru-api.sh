#!/usr/bin/env bash
set -Eeuo pipefail

readonly MINERU_INSTALL_APP_ROOT="/u01/aipoc/no.1-semantic-doc-search"
readonly MINERU_INSTALL_ROOT="/opt/no1-mineru-api"
readonly MINERU_INSTALL_VENV="${MINERU_INSTALL_ROOT}/venv"
readonly MINERU_INSTALL_STATE="/var/lib/no1-mineru-api"
readonly MINERU_INSTALL_CACHE="/var/cache/no1-mineru-api"
readonly MINERU_INSTALL_ENV_FILE="/etc/no1-mineru-api.env"
readonly MINERU_INSTALL_SERVICE_FILE="/etc/systemd/system/no1-mineru-api.service"
readonly MINERU_INSTALL_GPU_HOST="http://10.0.0.209:80"
readonly MINERU_INSTALL_MODEL="mineru2-5-pro-2605-1-2b"
readonly MINERU_INSTALL_VERSION="3.4.4"
readonly MINERU_INSTALL_ORIGINAL_CLIENT_SHA="c8b5435cf6fbed48bc161591b8219af8c281c55db2af0c18d6258e9e7b682c55"
readonly MINERU_INSTALL_PATCHED_CLIENT_SHA="4b837820b094150f1e86a00487ef0e622f3c7152530d874fe9d9bccfa3663e8e"

mineru_install_log() {
  printf '[MinerU installer] %s\n' "$*"
}

mineru_install_fail() {
  mineru_install_log "ERROR: $*" >&2
  exit 1
}

mineru_install_set_app_env() {
  local mineru_install_key="$1"
  local mineru_install_value="$2"
  local mineru_install_env_file="${MINERU_INSTALL_APP_ROOT}/.env"

  if grep -q "^${mineru_install_key}=" "${mineru_install_env_file}"; then
    sed -i "s|^${mineru_install_key}=.*$|${mineru_install_key}=${mineru_install_value}|" "${mineru_install_env_file}"
  else
    printf '%s=%s\n' "${mineru_install_key}" "${mineru_install_value}" >>"${mineru_install_env_file}"
  fi
}

if [[ "${EUID}" -ne 0 ]]; then
  mineru_install_fail "sudoで実行してください: sudo $0"
fi

[[ -d "${MINERU_INSTALL_APP_ROOT}" ]] || mineru_install_fail "アプリが見つかりません: ${MINERU_INSTALL_APP_ROOT}"
[[ -f "${MINERU_INSTALL_APP_ROOT}/.env" ]] || mineru_install_fail "アプリの.envが見つかりません"
id ubuntu >/dev/null 2>&1 || mineru_install_fail "ubuntuユーザーが見つかりません"

readonly MINERU_INSTALL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly MINERU_INSTALL_SOURCE_ROOT="$(cd "${MINERU_INSTALL_SCRIPT_DIR}/.." && pwd)"
readonly MINERU_INSTALL_SOURCE_CLIENT="${MINERU_INSTALL_SOURCE_ROOT}/backend/app/rag/clients.py"
readonly MINERU_INSTALL_LIVE_CLIENT="${MINERU_INSTALL_APP_ROOT}/backend/app/rag/clients.py"

[[ -f "${MINERU_INSTALL_SOURCE_CLIENT}" ]] || mineru_install_fail "変更済みclients.pyが見つかりません: ${MINERU_INSTALL_SOURCE_CLIENT}"

mineru_install_source_sha="$(sha256sum "${MINERU_INSTALL_SOURCE_CLIENT}" | awk '{print $1}')"
[[ "${mineru_install_source_sha}" == "${MINERU_INSTALL_PATCHED_CLIENT_SHA}" ]] || mineru_install_fail "変更済みclients.pyの内容が想定と異なります"

mineru_install_live_sha="$(sha256sum "${MINERU_INSTALL_LIVE_CLIENT}" | awk '{print $1}')"
case "${mineru_install_live_sha}" in
  "${MINERU_INSTALL_ORIGINAL_CLIENT_SHA}"|"${MINERU_INSTALL_PATCHED_CLIENT_SHA}") ;;
  *) mineru_install_fail "本番clients.pyに別の変更があります。安全のため上書きを停止しました" ;;
esac

printf 'GPUデプロイ時に設定したvLLM APIキーを入力してください（画面には表示されません）: '
IFS= read -r -s mineru_install_vllm_key
printf '\n'
[[ "${mineru_install_vllm_key}" =~ ^sk-[A-Za-z0-9._-]{8,}$ ]] || mineru_install_fail "APIキーはsk-で始まる値を入力してください"

mineru_install_log "GPUのプライベートAPIと認証を確認しています"
mineru_install_models_response="$(
  curl -fsS --connect-timeout 10 --max-time 30 \
    -H "Authorization: Bearer ${mineru_install_vllm_key}" \
    "${MINERU_INSTALL_GPU_HOST}/v1/models"
)" || mineru_install_fail "GPU APIへ接続できません。GPU側の初期化完了後に再実行してください"

printf '%s' "${mineru_install_models_response}" | grep -Fq "${MINERU_INSTALL_MODEL}" || mineru_install_fail "GPU APIに想定モデル ${MINERU_INSTALL_MODEL} がありません"
unset mineru_install_models_response

mineru_install_log "公式MinerU ${MINERU_INSTALL_VERSION} の軽量API環境を作成しています"
if ! dpkg-query -W -f='${Status}' python3.12-venv 2>/dev/null | grep -Fq 'ok installed'; then
  mineru_install_log "不足しているpython3.12-venvを導入しています"
  apt-get update
  apt-get install -y python3.12-venv
fi
install -d -m 0755 "${MINERU_INSTALL_ROOT}"
python3 -m venv --clear "${MINERU_INSTALL_VENV}"
"${MINERU_INSTALL_VENV}/bin/python" -m pip install --upgrade pip
"${MINERU_INSTALL_VENV}/bin/pip" install "mineru==${MINERU_INSTALL_VERSION}"

install -d -m 0750 -o ubuntu -g ubuntu "${MINERU_INSTALL_STATE}" "${MINERU_INSTALL_STATE}/output"
install -d -m 0750 -o ubuntu -g ubuntu "${MINERU_INSTALL_CACHE}"

mineru_install_tmp_dir="$(mktemp -d /tmp/no1-mineru-install.XXXXXX)"
trap 'rm -rf "${mineru_install_tmp_dir}"' EXIT

umask 077
{
  printf 'MINERU_VL_API_KEY=%s\n' "${mineru_install_vllm_key}"
  printf 'MINERU_VL_MODEL_NAME=%s\n' "${MINERU_INSTALL_MODEL}"
  printf 'MINERU_API_OUTPUT_ROOT=%s\n' "${MINERU_INSTALL_STATE}/output"
  printf 'MINERU_API_MAX_CONCURRENT_REQUESTS=1\n'
  printf 'MINERU_API_TASK_RETENTION_SECONDS=86400\n'
  printf 'MINERU_API_ENABLE_FASTAPI_DOCS=0\n'
  printf 'MINERU_LOG_LEVEL=INFO\n'
  printf 'XDG_CACHE_HOME=%s\n' "${MINERU_INSTALL_CACHE}"
  printf 'NO_PROXY=127.0.0.1,localhost,10.0.0.209\n'
} >"${mineru_install_tmp_dir}/no1-mineru-api.env"
unset mineru_install_vllm_key
install -m 0600 -o root -g root "${mineru_install_tmp_dir}/no1-mineru-api.env" "${MINERU_INSTALL_ENV_FILE}"

tee "${mineru_install_tmp_dir}/no1-mineru-api.service" >/dev/null <<SERVICE
[Unit]
Description=No.1 semantic document search MinerU API
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=${MINERU_INSTALL_STATE}
EnvironmentFile=${MINERU_INSTALL_ENV_FILE}
Environment=PYTHONUNBUFFERED=1
ExecStart=${MINERU_INSTALL_VENV}/bin/mineru-api --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
SERVICE
install -m 0644 -o root -g root "${mineru_install_tmp_dir}/no1-mineru-api.service" "${MINERU_INSTALL_SERVICE_FILE}"

mineru_install_timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
cp -a "${MINERU_INSTALL_APP_ROOT}/.env" "${MINERU_INSTALL_APP_ROOT}/.env.before-mineru-${mineru_install_timestamp}"
cp -a "${MINERU_INSTALL_LIVE_CLIENT}" "${MINERU_INSTALL_LIVE_CLIENT}.before-mineru-${mineru_install_timestamp}"
install -m 0644 -o root -g root "${MINERU_INSTALL_SOURCE_CLIENT}" "${MINERU_INSTALL_LIVE_CLIENT}"

mineru_install_set_app_env "MINERU_ENABLED" "true"
mineru_install_set_app_env "MINERU_API_HOST" "http://127.0.0.1:8000"
mineru_install_set_app_env "MINERU_API_TIMEOUT_SECONDS" "1800"
mineru_install_set_app_env "MINERU_BACKEND" "vlm-http-client"
mineru_install_set_app_env "MINERU_VLLM_API_HOST" "${MINERU_INSTALL_GPU_HOST}"
mineru_install_set_app_env "MINERU_IMAGE_ANALYSIS" "true"
chmod 0600 "${MINERU_INSTALL_APP_ROOT}/.env"

systemctl daemon-reload
systemctl enable --now no1-mineru-api.service

mineru_install_log "ローカルMinerU APIの起動を確認しています"
for mineru_install_attempt in $(seq 1 30); do
  if curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:8000/health >/dev/null; then
    break
  fi
  if [[ "${mineru_install_attempt}" -eq 30 ]]; then
    systemctl status no1-mineru-api.service --no-pager || true
    mineru_install_fail "ローカルMinerU APIが起動しませんでした"
  fi
  sleep 2
done

mineru_install_log "アプリのバックエンドへ設定を反映しています"
"${MINERU_INSTALL_APP_ROOT}/restart.sh"

mineru_install_log "完了しました。管理画面のMinerU接続テストを実行してください"
