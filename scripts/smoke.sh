#!/usr/bin/env bash
# Uçtan uca duman testi: örnek kaydı yükler, işlemesini bekler, özet + soru
# akışını dener ve sade özet çizgileri basar. Ollama konteynerine dokunmaz.
set -euo pipefail

ROOT="${SPEECH_STUDIO_ROOT:-$HOME/speech-studio}"
PORT="${SPEECH_STUDIO_PORT:-17841}"
KEY_FILE="$ROOT/library/.ui-token"
SAMPLE="${1:-$ROOT/build/samples/diarization_example.mp3}"
HOST_HDR="Host: localhost:$PORT"
BASE="http://127.0.0.1:$PORT"

[ -f "$SAMPLE" ] || { echo "örnek ses yok: $SAMPLE" >&2; exit 1; }
KEY=$(cat "$KEY_FILE" 2>/dev/null || true)
AUTH=(-H "$HOST_HDR")
if [ -n "$KEY" ]; then AUTH+=(-H "Authorization: Bearer $KEY"); fi

echo "== yükleme: $SAMPLE"
ID=$(curl -sS "${AUTH[@]}" -F "file=@$SAMPLE" "$BASE/api/recordings" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "   kayıt: $ID"

echo "== işlem bekleniyor"
for _ in $(seq 1 240); do
  STATE=$(curl -sS "${AUTH[@]}" "$BASE/api/recordings/$ID" \
    | python3 -c "import sys,json;j=json.load(sys.stdin);print(j['job']['state'])")
  printf "   %s        \r" "$STATE"
  case "$STATE" in
    done) break ;;
    failed) echo; curl -sS "${AUTH[@]}" "$BASE/api/recordings/$ID" | python3 -m json.tool | tail -8; exit 1 ;;
    *) sleep 5 ;;
  esac
done

echo "== sonuç"
curl -sS "${AUTH[@]}" "$BASE/api/recordings/$ID" -o /tmp/smoke-detail.json
python3 - /tmp/smoke-detail.json <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
result = j.get("result") or {}
turns = result.get("turns", [])
print("   süre           :", result.get("duration"), "sn")
print("   konuşmacı sayısı:", result.get("speaker_count"))
print("   tur sayısı     :", len(turns))
for turn in turns[:4]:
    print(f"   [{turn['start']:7.2f}-{turn['end']:7.2f}] S{turn['speaker']}: {turn['text'][:80]}")
PY

echo "== özet"
curl -sS "${AUTH[@]}" -X POST "$BASE/api/recordings/$ID/summary" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['summary'][:400])"

echo "== soru"
curl -sS "${AUTH[@]}" -H "Content-Type: application/json" -X POST \
  -d '{"question":"Bu kayıtta ana konu ne?"}' "$BASE/api/recordings/$ID/ask" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['answer'][:300])"

echo "== bitti: kayıt $ID  (arayüzde açın)"