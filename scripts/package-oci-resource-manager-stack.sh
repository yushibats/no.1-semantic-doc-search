#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_DIR="${REPOSITORY_ROOT}/terraform/stack"
OUTPUT_PATH="${1:-${REPOSITORY_ROOT}/releases/oci/case-plan-comparison-preview.zip}"
case "${OUTPUT_PATH}" in
  /*) ;;
  *) OUTPUT_PATH="${REPOSITORY_ROOT}/${OUTPUT_PATH}" ;;
esac
TEMP_DIR="$(mktemp -d)"
TEMP_ZIP="${TEMP_DIR}/stack.zip"
trap 'rm -rf "${TEMP_DIR}"' EXIT

python3 - "${STACK_DIR}" "${TEMP_ZIP}" <<'PY'
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import sys

stack_dir = Path(sys.argv[1])
output_path = Path(sys.argv[2])

with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as archive:
    for path in sorted(stack_dir.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(stack_dir).as_posix())
PY
unzip -tq "${TEMP_ZIP}"
mkdir -p "$(dirname "${OUTPUT_PATH}")"
mv "${TEMP_ZIP}" "${OUTPUT_PATH}"
printf 'Created OCI Resource Manager stack: %s\n' "${OUTPUT_PATH}"
