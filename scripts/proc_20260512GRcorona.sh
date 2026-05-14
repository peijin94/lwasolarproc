#!/usr/bin/env bash
set -euo pipefail

# Process the flat 20260512GRcorona event folder.
# Override settings at invocation time, e.g.:
#   WORKERS=8 START_TIMESTAMP=20260512_170000 ./scripts/proc_20260512GRcorona.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV_SETUP="${ENV_SETUP:-/fast/rtpipe/use_lwa.sh}"
PYTHON="${PYTHON:-/fast/rtpipe/env/lwa/bin/python}"

DATA_DIR="${DATA_DIR:-/lustre/solarpipe/20260512GRcorona}"
CALTAB_DIR="${CALTAB_DIR:-/fast/rtpipe/caltab_h5parm}"
PROC_TMP="${PROC_TMP:-/dev/shm/tmp_pipe_dir/proc_backlog}"
PROC_OUT="${PROC_OUT:-/scratch/event_proc/20260512GRcorona}"

START_TIMESTAMP="${START_TIMESTAMP:-20260512_160002}"
END_TIMESTAMP="${END_TIMESTAMP:-20260512_215508}"
CADENCE_S="${CADENCE_S:-10}"

BANDS="${BANDS:-23MHz,32MHz,41MHz,46MHz,50MHz,55MHz,59MHz,64MHz,69MHz,73MHz,78MHz,82MHz}"
READY_MIN_BANDS="${READY_MIN_BANDS:-7}"

WORKERS="${WORKERS:-8}"
PIPELINE_JOBS="${PIPELINE_JOBS:-13}"
THREADS="${THREADS:-18}"
FCH_POLS="${FCH_POLS:-I,V}"

if [[ -r "${ENV_SETUP}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_SETUP}"
fi

export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"

mkdir -p "${PROC_TMP}" "${PROC_OUT}"

cmd=(
  "${PYTHON}" -m lwasolarproc.realtime_task_manage
  --mode event
  --data-dir "${DATA_DIR}"
  --caltable-dir "${CALTAB_DIR}"
  --start-timestamp "${START_TIMESTAMP}"
  --end-timestamp "${END_TIMESTAMP}"
  --cadence-s "${CADENCE_S}"
  --bands "${BANDS}"
  --ready-min-bands "${READY_MIN_BANDS}"
  --workers "${WORKERS}"
  --pipeline-jobs "${PIPELINE_JOBS}"
  --threads "${THREADS}"
  --fch-pols "${FCH_POLS}"
  --proc-tmp "${PROC_TMP}"
  --proc-out "${PROC_OUT}"
)

printf 'Running:'
printf ' %q' "${cmd[@]}"
printf '\n'

exec "${cmd[@]}"
