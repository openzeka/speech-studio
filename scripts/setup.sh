#!/usr/bin/env bash
# speech-studio kurulum — Jetson (Ubuntu 22.04, aarch64, Docker + NVIDIA runtime).
# Yeniden çalıştırılabilir: sanal ortamı ve systemd birimini idempotent kurar.
# Ollama konteynerine, modellerine dokunmaz.
set -euo pipefail

ROOT="${SPEECH_STUDIO_ROOT:-$HOME/speech-studio}"
cd "$ROOT"

echo "[1/4] Python ortamı"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r server/requirements.txt

echo "[2/4] NeMo-Speech imajı kontrolü"
if docker image inspect "${SPEECH_NEMO_IMAGE:-speech-nemo:local}" >/dev/null 2>&1; then
  echo "      hazır: ${SPEECH_NEMO_IMAGE:-speech-nemo:local}"
else
  echo "      YOK — önce imajı derleyin (docs/KURULUM.md):"
  echo "      docker build -f vendor/NeMo-Speech.cpp/docker/Dockerfile --target runtime ..."
  echo "      [!] servis yine kurulur; ses işleme imaj gelene kadar hata verir."
fi

echo "[3/4] systemd birimi"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/speech-studio.service" <<UNIT
[Unit]
Description=Speech Studio (ses kaydı konuşmacı ayrımı + özet)

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python $ROOT/server/app.py
Restart=on-failure
RestartSec=5
Environment=SPEECH_STUDIO_ROOT=$ROOT
Environment=SPEECH_STUDIO_PORT=${SPEECH_STUDIO_PORT:-17841}
Environment=STUDIO_LLM_MODEL=${STUDIO_LLM_MODEL:-qwen2.5:3b}

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable -q speech-studio.service
systemctl --user restart speech-studio.service

echo "[4/4] Erişim"
sleep 1
KEY="$(cat "$ROOT/library/.ui-token" 2>/dev/null || true)"
PORT="${SPEECH_STUDIO_PORT:-17841}"
echo "  adres : http://127.0.0.1:$PORT/?key=$KEY"
echo "  tünel : ssh -N -L $PORT:127.0.0.1:$PORT <jetson>"
systemctl --user --no-pager status speech-studio.service | head -5