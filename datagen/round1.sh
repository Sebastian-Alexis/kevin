#!/usr/bin/env bash
# Round 1: everything that needs no human. Runs on any machine (the AX part is separate, see below).
#   bash datagen/round1.sh                  # full: ~4k osatlas + ~5k web + templates, ~1 h
#   QUICK=1 bash datagen/round1.sh          # smoke test, couple of minutes
# AX data comes from teammates' Macs: python datagen/ax_grounding.py watch|apps ... then rsync data/ax/ here.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
if [ -n "${QUICK:-}" ]; then N_OSA=200; N_WEB=200; N_TPL=150; else N_OSA=4000; N_WEB=5000; N_TPL=2500; fi

$PY datagen/osatlas_convert.py --os macos -n "$N_OSA" & OSA=$!
$PY datagen/web_grounding.py -n "$N_WEB" & WEB=$!
wait $OSA || { echo "osatlas failed: rerun with --resume" >&2; exit 1; }
wait $WEB || { echo "web grounding failed" >&2; exit 1; }
$PY datagen/action_templates.py --from osatlas,web,ax -n "$N_TPL"

if [ -n "${REPHRASE:-}" ]; then   # off for round 1 so we can measure its effect later
  $PY datagen/rephrase.py --source osatlas,web,ax --rate 0.6
else
  echo "rephrase off (REPHRASE=1 to enable): template intents only"
fi

$PY training/build_dataset.py --mix ax=0.3,web=0.25,osatlas=0.25,templates=0.2
$PY datagen/preview.py -n 60
