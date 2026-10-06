#!/usr/bin/env bash
# NeMo-Speech konteynerini elle çağırmak için ince sarmalayıcı.
#   scripts/nemo-run.sh transcribe ~/speech-studio/library/<id>/audio.wav --diarize --json
#   scripts/nemo-run.sh model list
# Verilen yolun bulunduğu klasör /work olarak bağlanır; model önbelleği kalıcıdır.
set -euo pipefail

ROOT="${SPEECH_STUDIO_ROOT:-$HOME/speech-studio}"
IMAGE="${SPEECH_NEMO_IMAGE:-speech-nemo:local}"

if [ "$#" -lt 1 ]; then
  echo "kullanım: scripts/nemo-run.sh <nemo-speech argümanları...>" >&2
  exit 2
fi

ARGS=()
MOUNT=""
for arg in "$@"; do
  if [ -f "$arg" ]; then
    dir="$(cd "$(dirname "$arg")" && pwd)"
    file="$(basename "$arg")"
    ARGS+=("/work/$file")
    if [ -z "$MOUNT" ]; then
      MOUNT="$dir:/work"
    fi
  else
    ARGS+=("$arg")
  fi
done

FLAGS=(--rm --gpus all --user "$(id -u):$(id -g)"
       -e HOME=/modelhome
       -v "$ROOT/model-cache:/modelhome/.cache")
# JetPack'ın kendi cuBLAS kütüphaneleri (Tegra için derlenmiş) imajın SBSA
# cuBLAS'ının yerine geçer — aksi halde cublasCreate GPU kaynak hatasıyla çöker.
if [ -f /usr/local/cuda/lib64/libcublas.so.12 ]; then
  FLAGS+=(-v /usr/local/cuda/lib64/libcublas.so.12:/opt/nemo-speech/lib/libcublas.so.12:ro
          -v /usr/local/cuda/lib64/libcublasLt.so.12:/opt/nemo-speech/lib/libcublasLt.so.12:ro)
fi
if [ -n "$MOUNT" ]; then
  FLAGS+=(-v "$MOUNT" -w /work)
fi

exec docker run "${FLAGS[@]}" "$IMAGE" "${ARGS[@]}"