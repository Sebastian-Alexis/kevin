#!/usr/bin/env bash
# Push code + data to the GPU box, pull checkpoints back.
#   GPU=root@1.2.3.4 training/sync.sh up      # data/ shared/ training/ datagen/
#   GPU=root@1.2.3.4 training/sync.sh down    # training/saves/ (adapters only, skips optimizer state)
set -euo pipefail
: "${GPU:?set GPU=user@host}"
DEST=${DEST:-ai-kvm}
cd "$(dirname "$0")/.."
case "${1:-up}" in
  up)   rsync -az --info=progress2 --exclude '_preview' --exclude '_cache' --exclude '*.bak' \
          data shared training datagen "$GPU:$DEST/" ;;
  down) rsync -az --info=progress2 --exclude 'optimizer.pt' --exclude 'global_step*' \
          "$GPU:$DEST/training/saves/" training/saves/ ;;
  *) echo "usage: $0 up|down" >&2; exit 1 ;;
esac
