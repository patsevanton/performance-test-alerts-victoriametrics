#!/bin/bash
# VMRule с глобальными индексами 1–1000 (sort -V). Каждый запуск — с начала диапазона.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export APPLY_BATCH_ID="01"
export APPLY_INDEX_START=1
export APPLY_INDEX_END=1000
# shellcheck source=apply-yaml-lib.sh
source "${SCRIPT_DIR}/apply-yaml-lib.sh"
run_batch "${SCRIPT_DIR}"
