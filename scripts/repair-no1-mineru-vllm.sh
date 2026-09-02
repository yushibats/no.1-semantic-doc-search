#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly MINERU_GPU_CONFIG="/etc/mineru-vllm-deployer/config.json"
readonly MINERU_GPU_ENV_FILE="/etc/mineru-vllm-deployer/vllm.env"
readonly MINERU_GPU_ROOT="/opt/mineru-vllm"
readonly MINERU_GPU_BACKUP="/var/lib/mineru-vllm-deployer/container-before-logits.json"
readonly MINERU_GPU_COMPAT_BASE_IMAGE="vllm/vllm-openai:v0.21.0"
readonly MINERU_GPU_CUSTOM_IMAGE="no1/mineru-vllm:v0.21.0-mineru-3.4.4"

mineru_gpu_log() {
  printf '[MinerU GPU repair] %s\n' "$*"
}

mineru_gpu_fail() {
  mineru_gpu_log "ERROR: $*" >&2
  exit 1
}

if [[ "${EUID}" -ne 0 ]]; then
  mineru_gpu_fail "GPUインスタンス上でsudoを付けて実行してください"
fi

for mineru_gpu_command in docker jq curl; do
  command -v "${mineru_gpu_command}" >/dev/null 2>&1 || mineru_gpu_fail "${mineru_gpu_command}が見つかりません"
done
[[ -r "${MINERU_GPU_CONFIG}" ]] || mineru_gpu_fail "${MINERU_GPU_CONFIG}が読めません"
[[ -r "${MINERU_GPU_ENV_FILE}" ]] || mineru_gpu_fail "${MINERU_GPU_ENV_FILE}が読めません"

mineru_gpu_application_port="$(jq -er '.application_port' "${MINERU_GPU_CONFIG}")"
mineru_gpu_base_image="$(jq -er '.vllm_image' "${MINERU_GPU_CONFIG}")"
mineru_gpu_container_name="$(jq -er '.vllm_container_name' "${MINERU_GPU_CONFIG}")"
mineru_gpu_dtype="$(jq -er '.dtype' "${MINERU_GPU_CONFIG}")"
mineru_gpu_expected_gpu_count="$(jq -er '.expected_gpu_count' "${MINERU_GPU_CONFIG}")"
mineru_gpu_memory_utilization="$(jq -er '.gpu_memory_utilization' "${MINERU_GPU_CONFIG}")"
mineru_gpu_hf_model_id="$(jq -er '.hf_model_id' "${MINERU_GPU_CONFIG}")"
mineru_gpu_max_model_len="$(jq -er '.max_model_len' "${MINERU_GPU_CONFIG}")"
mineru_gpu_max_num_seqs="$(jq -er '.max_num_seqs' "${MINERU_GPU_CONFIG}")"
mineru_gpu_served_model_name="$(jq -er '.served_model_name' "${MINERU_GPU_CONFIG}")"
mineru_gpu_trust_remote_code="$(jq -er '.trust_remote_code' "${MINERU_GPU_CONFIG}")"
mineru_gpu_vllm_api_key="$(jq -er '.vllm_api_key' "${MINERU_GPU_CONFIG}")"

[[ "${mineru_gpu_container_name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]+$ ]] || mineru_gpu_fail "コンテナ名が不正です"
[[ "${mineru_gpu_application_port}" =~ ^[0-9]+$ ]] || mineru_gpu_fail "APIポートが不正です"
[[ "${mineru_gpu_trust_remote_code}" == "true" ]] || mineru_gpu_fail "trust_remote_codeがtrueではありません"
docker container inspect "${mineru_gpu_container_name}" >/dev/null 2>&1 || mineru_gpu_fail "既存vLLMコンテナが見つかりません"

docker inspect "${mineru_gpu_container_name}" >"${MINERU_GPU_BACKUP}"

mineru_gpu_build_dir="$(mktemp -d /tmp/no1-mineru-gpu-repair.XXXXXX)"
trap 'rm -rf "${mineru_gpu_build_dir}"' EXIT
tee "${mineru_gpu_build_dir}/Dockerfile" >/dev/null <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN python3 -m pip install --no-cache-dir --break-system-packages \
    "mineru[vlm]==3.4.4" \
    "mineru-vl-utils==1.0.5"
DOCKERFILE

mineru_gpu_log "公式対応版vLLM 0.21.0とMinerU 3.4.4のイメージを作成しています"
docker build \
  --build-arg "BASE_IMAGE=${MINERU_GPU_COMPAT_BASE_IMAGE}" \
  --tag "${MINERU_GPU_CUSTOM_IMAGE}" \
  "${mineru_gpu_build_dir}"
docker run --rm --gpus all --entrypoint python3 "${MINERU_GPU_CUSTOM_IMAGE}" \
  -c 'from mineru_vl_utils import MinerULogitsProcessor; print(MinerULogitsProcessor.__name__)'

mineru_gpu_start_container() {
  local mineru_gpu_image="$1"
  local mineru_gpu_enable_processor="$2"
  local -a mineru_gpu_extra_args=()

  if [[ "${mineru_gpu_enable_processor}" == "true" ]]; then
    mineru_gpu_extra_args=(--logits-processors mineru_vl_utils:MinerULogitsProcessor)
  fi

  docker run -d \
    --name "${mineru_gpu_container_name}" \
    --gpus all \
    --ipc=host \
    -p "0.0.0.0:${mineru_gpu_application_port}:8000" \
    --env-file "${MINERU_GPU_ENV_FILE}" \
    -v "${MINERU_GPU_ROOT}/hf-cache:/root/.cache/huggingface" \
    -v "${MINERU_GPU_ROOT}/logs:/logs" \
    --restart unless-stopped \
    --log-opt max-size=100m \
    --log-opt max-file=5 \
    "${mineru_gpu_image}" \
    "${mineru_gpu_hf_model_id}" \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name "${mineru_gpu_served_model_name}" \
    --dtype "${mineru_gpu_dtype}" \
    --tensor-parallel-size "${mineru_gpu_expected_gpu_count}" \
    --gpu-memory-utilization "${mineru_gpu_memory_utilization}" \
    --max-num-seqs "${mineru_gpu_max_num_seqs}" \
    --max-model-len "${mineru_gpu_max_model_len}" \
    --trust-remote-code \
    "${mineru_gpu_extra_args[@]}"
}

mineru_gpu_wait_until_ready() {
  local mineru_gpu_attempt
  for mineru_gpu_attempt in {1..360}; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "${mineru_gpu_container_name}" 2>/dev/null || true)" != "true" ]]; then
      return 1
    fi
    if curl -fsS --connect-timeout 2 --max-time 5 \
      -H "Authorization: Bearer ${mineru_gpu_vllm_api_key}" \
      "http://127.0.0.1:${mineru_gpu_application_port}/v1/models" >/dev/null; then
      return 0
    fi
    sleep 10
  done
  return 1
}

mineru_gpu_log "既存コンテナを、修正版イメージで置き換えています"
docker rm -f "${mineru_gpu_container_name}"
if ! mineru_gpu_start_container "${MINERU_GPU_CUSTOM_IMAGE}" true >/dev/null; then
  mineru_gpu_log "修正版コンテナの作成に失敗したため元の構成へ戻します"
  docker rm -f "${mineru_gpu_container_name}" >/dev/null 2>&1 || true
  mineru_gpu_start_container "${mineru_gpu_base_image}" false >/dev/null
  mineru_gpu_fail "修正版コンテナを開始できませんでした"
fi

mineru_gpu_log "モデルの再ロードを待っています"
if ! mineru_gpu_wait_until_ready; then
  docker logs --tail 200 "${mineru_gpu_container_name}" || true
  mineru_gpu_log "修正版が起動しないため元の構成へロールバックします"
  docker rm -f "${mineru_gpu_container_name}" >/dev/null 2>&1 || true
  mineru_gpu_start_container "${mineru_gpu_base_image}" false >/dev/null
  mineru_gpu_fail "修正版vLLMが準備完了になりませんでした"
fi

unset mineru_gpu_vllm_api_key
mineru_gpu_log "完了しました。MinerULogitsProcessor付きvLLMが稼働しています"
