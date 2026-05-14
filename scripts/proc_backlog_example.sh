#!/usr/bin/env bash
set -euo pipefail

# Example bounded backlog replay for structured slow-data under /lustre/pipeline/slow.
# Override any setting at invocation time, e.g.:
#   START_TIMESTAMP=20260424_200004 END_TIMESTAMP=20260424_200254 WORKERS=3 ./scripts/proc_backlog_example.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV_SETUP="${ENV_SETUP:-/fast/rtpipe/use_lwa.sh}"
PYTHON="${PYTHON:-/fast/rtpipe/env/lwa/bin/python}"

SLOW_ROOT="${SLOW_ROOT:-/lustre/pipeline/slow}"
CALTAB_DIR="${CALTAB_DIR:-/fast/rtpipe/caltab_h5parm}"
PROC_TMP="${PROC_TMP:-/dev/shm/tmp_pipe_dir/proc_backlog}"
PROC_OUT="${PROC_OUT:-/fast/rtpipe/proc_backlog/proc_out}"

START_TIMESTAMP="${START_TIMESTAMP:-20260424_200004}"
END_TIMESTAMP="${END_TIMESTAMP:-20260424_200254}"
CADENCE_S="${CADENCE_S:-10}"

BANDS="${BANDS:-23MHz,32MHz,36MHz,41MHz,46MHz,50MHz,55MHz,59MHz,64MHz,69MHz,73MHz,78MHz,82MHz}"
TRIGGER_BAND="${TRIGGER_BAND:-55MHz}"
READY_MIN_BANDS="${READY_MIN_BANDS:-7}"

WORKERS="${WORKERS:-5}"
PIPELINE_JOBS="${PIPELINE_JOBS:-13}"
THREADS="${THREADS:-18}"
FCH_POLS="${FCH_POLS:-I}"

INGEST_LUSTRE="${INGEST_LUSTRE:-false}"
LUSTRE_INGEST_ROOT="${LUSTRE_INGEST_ROOT:-/lustre/solarpipe/realtime_pipeline}"

if [[ -r "${ENV_SETUP}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_SETUP}"
fi

export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"

mkdir -p "${PROC_TMP}"
if [[ "${INGEST_LUSTRE}" != "true" ]]; then
  mkdir -p "${PROC_OUT}"
fi

cmd=(
  "${PYTHON}" -m lwasolarproc.realtime_task_manage
  --mode backlog
  --slow-root "${SLOW_ROOT}"
  --caltable-dir "${CALTAB_DIR}"
  --start-timestamp "${START_TIMESTAMP}"
  --end-timestamp "${END_TIMESTAMP}"
  --cadence-s "${CADENCE_S}"
  --bands "${BANDS}"
  --trigger-band "${TRIGGER_BAND}"
  --ready-min-bands "${READY_MIN_BANDS}"
  --workers "${WORKERS}"
  --pipeline-jobs "${PIPELINE_JOBS}"
  --threads "${THREADS}"
  --fch-pols "${FCH_POLS}"
  --proc-tmp "${PROC_TMP}"
  --proc-out "${PROC_OUT}"
)

if [[ "${INGEST_LUSTRE}" == "true" ]]; then
  cmd+=(--ingest-lustre --lustre-ingest-root "${LUSTRE_INGEST_ROOT}")
fi

printf 'Running:'
printf ' %q' "${cmd[@]}"
printf '\n'

exec "${cmd[@]}"
