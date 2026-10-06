#!/usr/bin/env bash
# Speech Studio — one-command installer for NVIDIA Jetson (Ubuntu 22.04, aarch64).
#
#   git clone <this repo> && cd speech-studio && bash install.sh
#
# What it does (nothing is ever deleted):
#   * checks prerequisites (docker + NVIDIA runtime, ffmpeg, git, curl, python3)
#   * clones NVIDIA/NeMo-Speech.cpp and applies the small platform adaptations
#     that CUDA 12.2 / Tegra builds require (see docs/INSTALL.md)
#   * builds the speech-nemo:local container image
#   * downloads the speech models YOU choose (ASR + diarization), verified
#     against the pinned size and SHA-256 in NeMo-Speech.cpp's model index
#   * installs the LLM you choose into your running Ollama container
#   * installs the local web console (systemd user service)
#
# Flags (all optional; interactive menu when omitted):
#   --asr  <model>     ASR model (nemotron-3.5 | parakeet-tdt | parakeet-ctc | nemotron-en)
#   --diar <model>     diarization (nemotron-3-diarization | sortformer)
#   --llm  <model>     Ollama LLM (e.g. qwen2.5:3b). Use --skip-llm to postpone.
#   --port <n>         console port (default 17841)
#   --jobs <n>         build parallelism (default 4)
#   --non-interactive  no prompts; use flags/defaults
#   --dry-run          print the plan and exit; changes nothing
#   -h | --help        this text
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASR_MODEL=""
DIAR_MODEL=""
LLM_MODEL=""
PORT="${SPEECH_STUDIO_PORT:-17841}"
JOBS="${SPEECH_BUILD_JOBS:-4}"
INTERACTIVE=1
DRY_RUN=0
SKIP_LLM=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --asr) ASR_MODEL="$2"; shift 2 ;;
    --diar) DIAR_MODEL="$2"; shift 2 ;;
    --llm) LLM_MODEL="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --non-interactive) INTERACTIVE=0; shift ;;
    --skip-llm) SKIP_LLM=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$*"; }
ok()  { printf '   %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight

preflight() {
  say "Preflight"
  command -v git >/dev/null || die "git is required"
  command -v curl >/dev/null || die "curl is required"
  command -v ffmpeg >/dev/null || die "ffmpeg is required"
  command -v python3 >/dev/null || die "python3 is required"
  command -v docker >/dev/null || die "docker is required"
  if docker info 2>/dev/null | grep -qi 'nvidia'; then
    ok "NVIDIA container runtime present"
  else
    die "NVIDIA Container Toolkit missing — GPU containers will not run (docs/INSTALL.md)"
  fi
  ARCH="$(uname -m)"; ok "arch: $ARCH"
  [ "$ARCH" = "aarch64" ] || ok "warning: validated on Jetson aarch64; on $ARCH you may need to adjust CUDA_ARCH"
}

# ------------------------------------------------------------------ source

seed_source() {
  local src="$ROOT/vendor/NeMo-Speech.cpp"
  say "NeMo-Speech.cpp source"
  if [ -d "$src/.git" ]; then
    ok "already present: $src"
  else
    mkdir -p "$ROOT/vendor"
    git clone --depth 1 https://github.com/NVIDIA/NeMo-Speech.cpp.git "$src"
    git -C "$src" submodule update --init --recursive --depth 1
    ok "cloned"
  fi
}

# Build-time adaptations for CUDA 12.2 / Tegra / Ubuntu 22.04. Idempotent.
# Rationale for each edit: docs/INSTALL.md
apply_adaptations() {
  local df="$ROOT/vendor/NeMo-Speech.cpp/docker/Dockerfile"
  say "Platform adaptations (idempotent)"
  [ -f "$df.orig" ] || cp "$df" "$df.orig"

  python3 - "$df" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
t = p.read_text()
edits = [
    # Ubuntu 22.04 has no gcc-13 packages; gcc-12 compiles this codebase fine.
    ("g++-13 \\", "g++-12 \\"),
    ("gcc-13 \\", "gcc-12 \\"),
    ("gcc gcc /usr/bin/gcc-13", "gcc gcc /usr/bin/gcc-12"),
    ("g++ g++ /usr/bin/g++-13", "g++ g++ /usr/bin/g++-12"),
    # jammy ships cmake 3.22 < the required 3.26.
    ('    && rm -rf /var/lib/apt/lists/*',
     '    && rm -rf /var/lib/apt/lists/* \\\n'
     '    && python3 -m pip install --no-cache-dir "cmake==3.31.6"'),
    # NVIDIA arm64 images stage CUDA libs under targets/sbsa-linux; the
    # loader-path symlink loop must include it.
    ("for cuda_target in x86_64-linux aarch64-linux; do",
     "for cuda_target in x86_64-linux aarch64-linux sbsa-linux; do"),
    # ggml-cuda needs full cuBLAS symbols (e.g. cublasSetWorkspace_v2).
    ("    && cp /work/LICENSE /work/NOTICE /work/THIRD_PARTY_NOTICES.md",
     "    && cp -a /usr/local/cuda/lib64/libcublas.so* /usr/local/cuda/lib64/libcublasLt.so* /out/lib/ \\\n"
     "    && cp /work/LICENSE /work/NOTICE /work/THIRD_PARTY_NOTICES.md"),
]
for old, new in edits:
    if old in t and new not in t:
        t = t.replace(old, new, 1)
p.write_text(t)
print("   adaptations up to date")
PY
}

# ------------------------------------------------------------------- image

build_image() {
  say "Building image speech-nemo:local (first build takes a while)"
  if docker image inspect speech-nemo:local >/dev/null 2>&1; then
    ok "image already present; force a rebuild with: docker rmi speech-nemo:local"
    return
  fi
  local cuda_arch=""
  [ "$(uname -m)" = "aarch64" ] && cuda_arch=87   # Jetson Orin (sm_87)
  docker build -f "$ROOT/vendor/NeMo-Speech.cpp/docker/Dockerfile" --target runtime \
    --build-arg CUDA_VERSION=12.2.0 \
    --build-arg UBUNTU_VERSION=22.04 \
    ${cuda_arch:+--build-arg CUDA_ARCH=$cuda_arch} \
    --build-arg ENABLE_GRPC=OFF \
    --build-arg ENABLE_HTTP=OFF \
    --build-arg ENABLE_NMT=OFF \
    --build-arg ENABLE_S2S=OFF \
    --build-arg ENABLE_TTS_JA=OFF \
    --build-arg ENABLE_TTS_ZH=OFF \
    --build-arg JOBS="$JOBS" \
    -t speech-nemo:local "$ROOT/vendor/NeMo-Speech.cpp"
  ok "image ready"
}

# --------------------------------------------------------------- model picks

INDEX_JSON="$ROOT/vendor/NeMo-Speech.cpp/models/index.json"

model_artifact() {  # prints: repo<TAB>revision<TAB>filename<TAB>sha256<TAB>size
  python3 - "$INDEX_JSON" "$1" <<'PY'
import json, sys
index = json.load(open(sys.argv[1]))
alias = sys.argv[2]
for m in index["models"]:
    if alias in m.get("aliases", []) or m["repo"].endswith(alias) or alias in m["repo"]:
        a = m["artifacts"][0]
        print("\t".join([m["repo"], m["revision"], a["filename"], a["sha256"], str(a["size"])]))
        break
PY
}

pick_models() {
  say "Model selection"
  if [ -z "$ASR_MODEL" ]; then
    if [ "$INTERACTIVE" = 1 ]; then
      echo "  ASR (speech -> text):"
      echo "    1) nemotron-3.5    — default, multilingual, streaming-capable"
      echo "    2) parakeet-tdt    — highest offline accuracy"
      echo "    3) parakeet-ctc    — fast, language-model boosted"
      echo "    4) nemotron-en     — English-only, smallest"
      printf "  choice [1]: "; read -r pick; case "${pick:-1}" in
        2) ASR_MODEL=parakeet-tdt ;; 3) ASR_MODEL=parakeet-ctc ;; 4) ASR_MODEL=nemotron-en ;;
        *) ASR_MODEL=nemotron-3.5 ;;
      esac
    else
      ASR_MODEL=nemotron-3.5
    fi
  fi
  if [ -z "$DIAR_MODEL" ]; then
    if [ "$INTERACTIVE" = 1 ]; then
      echo "  Diarization (who spoke when):"
      echo "    1) nemotron-3-diarization — default, up to 8 speakers"
      echo "    2) sortformer             — older 4-speaker model"
      printf "  choice [1]: "; read -r pick; case "${pick:-1}" in
        2) DIAR_MODEL=sortformer ;; *) DIAR_MODEL=nemotron-3-diarization ;;
      esac
    else
      DIAR_MODEL=nemotron-3-diarization
    fi
  fi
  ok "ASR: $ASR_MODEL"
  ok "diarization: $DIAR_MODEL"
}

fetch_speech_models() {
  say "Downloading speech models (SHA-256 verified)"
  [ -f "$INDEX_JSON" ] || die "model index missing: $INDEX_JSON"
  local cache="$ROOT/model-cache/nemo-speech/models"
  local name spec repo rev file sha size target url
  for name in "$ASR_MODEL" "$DIAR_MODEL"; do
    spec="$(model_artifact "$name")"
    [ -n "$spec" ] || die "unknown model: $name (see $INDEX_JSON)"
    IFS=$'\t' read -r repo rev file sha size <<<"$spec"
    target="$cache/$repo/$rev/$file"
    if [ -f "$target" ] && [ "$(sha256sum "$target" | cut -d' ' -f1)" = "$sha" ]; then
      ok "already present: $file"
      continue
    fi
    mkdir -p "$(dirname "$target")"
    url="https://huggingface.co/${repo}/resolve/${rev}/${file}?download=true"
    ok "downloading $file ($((size / 1024 / 1024)) MB)"
    curl -L --fail --retry 3 -sS -o "$target.part" "$url"
    mv "$target.part" "$target"
    echo "   sha256: $(sha256sum "$target" | cut -d' ' -f1)"
  done
}

# ------------------------------------------------------------- Ollama (LLM)

OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"

ollama_ready() { curl -s --max-time 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; }

try_start_ollama() {
  # Only ever *starts* a container; never stops or removes anything.
  local c
  c="$(docker ps -a --filter "name=ollama" --format '{{.ID}}' | head -1)"
  [ -n "$c" ] || c="$(docker ps -a --filter "ancestor=ollama/ollama" --format '{{.ID}}' | head -1)"
  if [ -n "$c" ]; then
    ok "found an ollama container; starting it (start only)"
    docker start "$c" >/dev/null
    local i
    for i in $(seq 1 30); do ollama_ready && return 0; sleep 1; done
  fi
  return 1
}

install_llm() {
  say "LLM setup — Ollama"
  if ! ollama_ready; then
    if ! try_start_ollama; then
      ok "note: Ollama not reachable at $OLLAMA_URL."
      ok "Start your container (docker start <ollama-container>) and finish later:"
      ok "  bash install.sh --llm <model>"
      return 0
    fi
  fi
  local installed
  installed="$(curl -s --max-time 5 "$OLLAMA_URL/api/tags" | python3 -c "
import json,sys
try: print(', '.join(m.get('name','?') for m in json.load(sys.stdin).get('models', [])))
except Exception: print('')" || true)"
  ok "models already in Ollama: ${installed:-none}"
  if [ -z "$LLM_MODEL" ]; then
    if [ "$INTERACTIVE" = 1 ]; then
      echo "  Name the model to use for summary/Q&A (e.g. qwen2.5:3b)."
      printf "  model: "; read -r LLM_MODEL
    fi
    [ -z "$LLM_MODEL" ] && { ok "no LLM chosen; skipping."; return 0; }
  fi
  ok "pulling into Ollama: $LLM_MODEL (existing models are untouched)"
  curl -s --max-time 3600 "$OLLAMA_URL/api/pull" \
    -d "$(python3 -c "import json,sys;print(json.dumps({'name': sys.argv[1], 'stream': False}))" "$LLM_MODEL")" \
    >/dev/null
  ok "LLM ready: $LLM_MODEL"
}

# --------------------------------------------------------------- web console

install_service() {
  say "Web console"
  [ -d "$ROOT/.venv" ] || python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q --upgrade pip
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/server/requirements.txt"

  local unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  cat > "$unit_dir/speech-studio.service" <<UNIT
[Unit]
Description=Speech Studio — local speaker diarization console

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python $ROOT/server/app.py
Restart=on-failure
RestartSec=5
Environment=SPEECH_STUDIO_ROOT=$ROOT
Environment=SPEECH_STUDIO_PORT=$PORT
Environment=STUDIO_LLM_MODEL=${LLM_MODEL:-qwen2.5:3b}

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable -q speech-studio.service
  systemctl --user restart speech-studio.service
  sleep 1
  local key=""
  key="$(cat "$ROOT/library/.ui-token" 2>/dev/null || true)"
  say "Done"
  ok "url    : http://127.0.0.1:$PORT/?key=$key"
  ok "tunnel : ssh -N -L $PORT:127.0.0.1:$PORT <you>@<your-jetson>"
  ok "check  : bash scripts/smoke.sh"
  ok "status : systemctl --user status speech-studio"
}

# ------------------------------------------------------------------- flow

say "Speech Studio install"
ok "root: $ROOT"
ok "picks -> ASR: ${ASR_MODEL:-<menu>}  diar: ${DIAR_MODEL:-<menu>}  LLM: ${LLM_MODEL:-<menu/skip>}"

if [ "$DRY_RUN" = 1 ]; then
  ok "dry-run: plan only; nothing was changed"
  exit 0
fi

preflight
seed_source
apply_adaptations
build_image
pick_models
fetch_speech_models
if [ "$SKIP_LLM" = 1 ]; then
  say "LLM step skipped (--skip-llm)"
else
  install_llm
fi
install_service