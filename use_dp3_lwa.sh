#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

. "$ROOT/use_lwa.sh"

export PATH="/opt/dp3-6.5.1/bin:$PATH"
export PYTHONPATH="/opt/dp3-6.5.1/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export CASARCFILES="${CASARCFILES:-$HOME/.casarc}"

