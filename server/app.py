#!/usr/bin/env python3
"""speech-studio — ses kaydı istemcisi/konsolu.

Bir ses kaydı yüklenir; NeMo-Speech.cpp konteyneri (Nemotron-3-Diarization +
ASR) konuşmacı ayrımı ve konuşmacı etiketli transkript üretir. Özet ve soru
cevaplama, mevcut Ollama konteynerindeki yerel modelden alınır.

Yalnızca 127.0.0.1'e bağlanır. Uzak erişim, kullanıcının açtığı SSH tüneliyle
olur; erişim anahtarı (library/.ui-token) tarayıcı sekmesinde saklanır.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

ROOT = Path(os.environ.get("SPEECH_STUDIO_ROOT", Path(__file__).resolve().parent.parent))
LIBRARY = ROOT / "library"
MODEL_CACHE = ROOT / "model-cache"
NEMO_IMAGE = os.environ.get("SPEECH_NEMO_IMAGE", "speech-nemo:local")
STUDIO_PORT = int(os.environ.get("SPEECH_STUDIO_PORT", "17841"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
LLM_MODEL = os.environ.get("STUDIO_LLM_MODEL", "qwen2.5:3b")

ALLOWED_AUDIO = {".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a"}
MAX_UPLOAD = 500 * 1024 * 1024
SPEAKER_GAP = 0.8          # >= bu süre boşluk → yeni konuşma turu
MAX_LLM_INPUT = 14_000     # transkriptten modele gidecek üst sınır (yaklaşık)

HOSTS = {f"localhost:{STUDIO_PORT}", f"127.0.0.1:{STUDIO_PORT}"}

job_lock = threading.Lock()
job_queue: list[str] = []
_active = {"id": None}


# ---------------------------------------------------------------- yardımcılar

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def rec_dir(rid: str) -> Path:
    return LIBRARY / rid


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def store(rid: str) -> dict:
    d = rec_dir(rid)
    meta = load_json(d / "meta.json", None)
    if meta is None:
        raise HTTPException(404, "Kayıt bulunamadı.")
    return {
        "meta": meta,
        "job": load_json(d / "job.json", {"state": "unknown"}),
        "result": load_json(d / "result.json", None),
        "speakers": load_json(d / "speakers.json", {}),
        "summary": (d / "summary.md").read_text() if (d / "summary.md").exists() else "",
        "questions": load_json(d / "questions.json", []),
    }


def write_job(rid: str, state: str, detail: str = "") -> None:
    (rec_dir(rid) / "job.json").write_text(
        json.dumps({"state": state, "detail": detail, "updated": now_iso()}))


def mmss(seconds: float) -> str:
    s = int(max(0.0, seconds))
    return f"{s // 60:02}:{s % 60:02}"


# --------------------------------------------------------- NeMo-Speech köprüsü

def nemo_command(rec: Path) -> list[str]:
    """Konteyneri sahip kullanıcısıyla çalıştırır; çıktı sahipliği host'ta kalır.

    JetPack'in kendi cuBLAS kütüphaneleri (Tegra derlemesi) imajın içindeki
    SBSA cuBLAS'ının yerine bağlanır; aksi halde `cublasCreate` kaynak hatası
    ile çöker (bkz. docs/KURULUM.md)."""
    uid = os.getuid()
    gid = os.getgid()
    cmd = [
        "docker", "run", "--rm", "--gpus", "all",
        "--user", f"{uid}:{gid}",
        "-e", "HOME=/modelhome",
        "-v", f"{MODEL_CACHE}:/modelhome/.cache",
    ]
    host_cublas = Path("/usr/local/cuda/lib64/libcublas.so.12")
    host_cublas_lt = Path("/usr/local/cuda/lib64/libcublasLt.so.12")
    if host_cublas.is_file() and host_cublas_lt.is_file():
        cmd += [
            "-v", f"{host_cublas}:/opt/nemo-speech/lib/libcublas.so.12:ro",
            "-v", f"{host_cublas_lt}:/opt/nemo-speech/lib/libcublasLt.so.12:ro",
        ]
    cmd += [
        "-v", f"{rec}:/work", "-w", "/work",
        NEMO_IMAGE,
        "transcribe", "audio.wav", "--diarize", "--json",
    ]
    return cmd


def parse_transcription(payload) -> tuple[list[dict], list[dict]]:
    """CLI JSON çıktısından (kelime → konuşmacı) ve turlara dönüştürür.

    `nemo-speech transcribe --diarize --json` her kelimeye 1-tabanlı bir
    `speaker` değeri koyar. Farklı sürümlerde biçim `words` ya da doğrudan liste
    olabilir; ikisi de kabul edilir.
    """
    words_raw = []
    if isinstance(payload, dict):
        for key in ("words", "result", "transcription", "segments"):
            value = payload.get(key)
            if isinstance(value, list):
                words_raw = value
                break
    elif isinstance(payload, list):
        words_raw = payload

    words: list[dict] = []
    for item in words_raw:
        if not isinstance(item, dict):
            continue
        text = item.get("word") or item.get("text") or item.get("value") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        start = float(item.get("start", item.get("start_time", item.get("beg", 0.0))) or 0.0)
        end = float(item.get("end", item.get("end_time", item.get("stop", start))) or start)
        speaker = item.get("speaker", item.get("spk", item.get("speaker_id", 0)))
        try:
            speaker = int(speaker)
        except (TypeError, ValueError):
            speaker = 0
        words.append({"start": round(start, 3), "end": round(end, 3),
                      "speaker": speaker, "text": text.strip()})

    words.sort(key=lambda w: (w["start"], w["end"]))
    turns: list[dict] = []
    for word in words:
        if turns and word["speaker"] == turns[-1]["speaker"] \
                and word["start"] - turns[-1]["end"] <= SPEAKER_GAP:
            turns[-1]["end"] = word["end"]
            turns[-1]["text"] = (turns[-1]["text"] + " " + word["text"]).strip()
        else:
            turns.append({"start": word["start"], "end": word["end"],
                          "speaker": word["speaker"], "text": word["text"]})
    return words, turns


def transcription_stub_ok(words: list[dict]) -> bool:
    return len(words) > 0


# --------------------------------------------------------------- özet + sorular

def llm_chat(messages: list[dict], num_predict: int = 700) -> str:
    body = {
        "model": LLM_MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": "5m",
        "options": {"temperature": 0.2, "num_ctx": 4096, "num_predict": num_predict},
    }
    request = urllib.request.Request(
        OLLAMA_URL + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"Model sunucusuna ulaşılamadı: {exc}") from exc
    if payload.get("done_reason") == "length":
        raise HTTPException(502, "Model yanıt sınırına ulaştı; özet kısaltılamadı.")
    content = (payload.get("message") or {}).get("content", "").strip()
    if not content:
        raise HTTPException(502, "Model boş yanıt üretti.")
    return content


def transcript_context(turns: list[dict]) -> str:
    lines = [f"[{mmss(t['start'])}] Konuşmacı {t['speaker']}: {t['text']}" for t in turns]
    text = "\n".join(lines)
    return text[:MAX_LLM_INPUT]


SUMMARY_SYSTEM = (
    "Sen ses kaydı özetleyicisisin. Yalnızca verilen transkript parçalarına "
    "dayan; olmayan bilgi, karar veya isim uydurma. Türkçe yanıt ver. Yanıtı "
    "üç başlıkla düzenla: Kısa özet (iki cümle), Önemli noktalar (madde "
    "işaretleri), Kararlar ve eylemler (yoksa sadece '—' yaz). Başlıkları "
    "yalnız birer kez yaz.")

QA_SYSTEM = (
    "Sen ses kaydı asistanısın. Yalnızca verilen transkript parçalarına dayan; "
    "emin değilsen 'Bu kayıtta geçmiyor' de. Türkçe yanıt ver ve dayandığın "
    "zaman damgalarını [mm:ss] şeklinde göster.")


async def make_summary(rid: str) -> str:
    d = rec_dir(rid)
    existing = d / "summary.md"
    if existing.exists():
        return existing.read_text()
    result = load_json(d / "result.json", None)
    if not result or not result.get("turns"):
        raise HTTPException(409, "Önce dijarizasyon/transkript tamamlanmalı.")
    summary = await asyncio.to_thread(llm_chat, [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": "Transkript:\n\n" + transcript_context(result["turns"])},
    ])
    existing.write_text(summary)
    return summary


# --------------------------------------------------------------- işleme kuyruğu

def process(rid: str) -> None:
    d = rec_dir(rid)
    try:
        write_job(rid, "processing", "diarizasyon + transkript")
        proc = subprocess.run(nemo_command(d), capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[-600:] or "nemo-speech hata verdi")
        payload = json.loads(proc.stdout)
        words, turns = parse_transcription(payload)
        if not transcription_stub_ok(words):
            raise RuntimeError("Model kelimeli çıktı üretmedi; ses boş ya da biçim desteklenmiyor.")
        with wave.open(str(d / "audio.wav")) as wav:
            duration = wav.getnframes() / wav.getframerate()
        (d / "result.json").write_text(json.dumps(
            {"words": words, "turns": turns,
             "duration": round(duration, 2),
             "speaker_count": len({w["speaker"] for w in words})},
            ensure_ascii=False))
        speakers = load_json(d / "speakers.json", {})
        (d / "speakers.json").write_text(json.dumps(speakers))
        write_job(rid, "done", "")
    except Exception as exc:  # hata kaydı kullanıcıya görünür, kayıt korunur
        write_job(rid, "failed", str(exc)[:600])


def pump() -> None:
    while True:
        with job_lock:
            rid = next((j for j in job_queue), None)
            if rid is None:
                _active["id"] = None
            else:
                job_queue.remove(rid)
                _active["id"] = rid
        if rid is None:
            threading.Event().wait(0.5)
            continue
        process(rid)


threading.Thread(target=pump, daemon=True).start()


# ------------------------------------------------------------------------- API

app = FastAPI(title="Speech Studio", docs_url=None, redoc_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    host = request.headers.get("host", "")
    if host not in HOSTS:
        return PlainTextResponse("Foreign Host refused.", status_code=403)
    origin = request.headers.get("origin")
    if origin and not any(origin == f"http://{h}" for h in HOSTS):
        return PlainTextResponse("Foreign origin refused.", status_code=403)
    return await call_next(request)


@app.get("/")
async def index():
    response = FileResponse(Path(__file__).parent / "static" / "index.html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/static/{name}")
async def static_file(name: str):
    path = Path(__file__).parent / "static" / name
    if not path.is_file() or "/" in name:
        raise HTTPException(404)
    media = {"app.js": "text/javascript", "style.css": "text/css",
             "favicon.svg": "image/svg+xml"}.get(
        name, "application/octet-stream")
    response = FileResponse(path, media_type=media + "; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/status")
async def status(request: Request):
    ollama = {"reachable": False, "model": LLM_MODEL}
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=3) as resp:
            models = [m.get("name") for m in json.loads(resp.read()).get("models", [])]
        ollama = {"reachable": True, "model": LLM_MODEL, "models": models}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        pass
    listings = []
    for meta_path in sorted(LIBRARY.glob("*/meta.json"), reverse=True):
        rid = meta_path.parent.name
        item = store(rid)
        jobs = item["job"]
        result = item["result"] or {}
        listings.append({
            "id": rid, "title": item["meta"].get("title", rid),
            "created": item["meta"].get("created"),
            "state": jobs.get("state", "unknown"), "detail": jobs.get("detail", ""),
            "duration": result.get("duration"),
            "speaker_count": result.get("speaker_count"),
        })
    return {"library": str(LIBRARY), "nemo_image": NEMO_IMAGE,
            "ollama": ollama, "queue": len(job_queue),
            "active": _active["id"], "recordings": listings}


@app.post("/api/recordings")
async def create(request: Request, file: UploadFile = File(...)):
    suffix = Path(file.filename or "recording").suffix.lower()
    if suffix not in ALLOWED_AUDIO:
        raise HTTPException(415, "Desteklenmeyen ses biçimi (wav/mp3/flac/ogg/opus/m4a).")
    rid = uuid.uuid4().hex[:12]
    d = rec_dir(rid)
    d.mkdir(parents=True)
    source = d / f"source{suffix}"
    total = 0
    with source.open("wb") as out:
        while chunk := await file.read(1 << 18):
            total += len(chunk)
            if total > MAX_UPLOAD:
                out.close()
                shutil.rmtree(d)
                raise HTTPException(413, "Dosya 500 MB sınırını aşıyor.")
            out.write(chunk)

    # 16 kHz mono PCM: hem model girdisi hem dalgı formu için tek kanonik biçim.
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(d / "audio.wav")],
        capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(d)
        raise HTTPException(422, "Ses çözülemedi: " + proc.stderr.strip()[-300:])

    title = Path(file.filename or "Kayıt").stem
    (d / "meta.json").write_text(json.dumps(
        {"id": rid, "title": title, "created": now_iso()}, ensure_ascii=False))
    write_job(rid, "queued", "sırada")
    with job_lock:
        job_queue.append(rid)
    return {"id": rid, "title": title}


@app.get("/api/recordings/{rid}")
async def detail(request: Request, rid: str):
    return store(rid)


@app.get("/api/recordings/{rid}/audio")
async def audio(rid: str):
    path = rec_dir(rid) / "audio.wav"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/recordings/{rid}/peaks")
async def peaks(request: Request, rid: str):
    cached = rec_dir(rid) / "peaks.json"
    if cached.is_file():
        return json.loads(cached.read_text())
    source = rec_dir(rid) / "audio.wav"
    if not source.is_file():
        raise HTTPException(404)
    with wave.open(str(source)) as wav:
        rate = wav.getframerate()
        width = wav.getsampwidth()
        frames = wav.getnframes()
        raw = wav.readframes(frames)
    if width != 2:
        raise HTTPException(422, "Beklenmeyen örnek genişliği.")
    count = min(1200, max(24, frames // 160))
    step = max(1, frames // count)
    data = []
    for i in range(count):
        chunk = raw[(i * step) * 2:(i + 1) * step * 2]
        nums = [int.from_bytes(chunk[j:j + 2], "little", signed=True)
                for j in range(0, len(chunk) - 1, 2)]
        data.append(round(math.sqrt(sum(n * n for n in nums) / max(1, len(nums))) / 32768, 3))
    payload = {"rate": rate, "frames": frames, "peaks": data}
    cached.write_text(json.dumps(payload))
    return payload


@app.post("/api/recordings/{rid}/summary")
async def summary(request: Request, rid: str):
    text = await make_summary(rid)
    return {"summary": text}


@app.post("/api/recordings/{rid}/ask")
async def ask(request: Request, rid: str):
    body = await request.json()
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "Soru gerekli.")
    result = load_json(rec_dir(rid) / "result.json", None)
    if not result or not result.get("turns"):
        raise HTTPException(409, "Kayıt henüz işlenmedi.")
    answer = await asyncio.to_thread(llm_chat, [
        {"role": "system", "content": QA_SYSTEM},
        {"role": "user", "content":
            "Transkript:\n\n" + transcript_context(result["turns"]) +
            "\n\nSoru: " + question},
    ])
    history = load_json(rec_dir(rid) / "questions.json", [])
    history.append({"question": question, "answer": answer, "at": now_iso()})
    (rec_dir(rid) / "questions.json").write_text(json.dumps(history, ensure_ascii=False))
    return {"answer": answer, "question": question}


@app.post("/api/recordings/{rid}/speakers")
async def speakers(request: Request, rid: str):
    body = await request.json()
    names = body.get("names")
    if not isinstance(names, dict):
        raise HTTPException(400, "İsim haritası gerekli.")
    mapping = {str(k): str(v)[:40] for k, v in names.items()}
    (rec_dir(rid) / "speakers.json").write_text(json.dumps(mapping, ensure_ascii=False))
    return {"speakers": mapping}


@app.post("/api/recordings/{rid}/title")
async def retitle(request: Request, rid: str):
    body = await request.json()
    title = (body.get("title") or "").strip()[:80]
    if not title:
        raise HTTPException(400, "Başlık gerekli.")
    meta = load_json(rec_dir(rid) / "meta.json", None)
    if meta is None:
        raise HTTPException(404)
    meta["title"] = title
    (rec_dir(rid) / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    return {"title": title}


@app.delete("/api/recordings/{rid}")
async def delete(request: Request, rid: str):
    if _active["id"] == rid:
        raise HTTPException(409, "İşlem sürüyor; bitince silinebilir.")
    target = rec_dir(rid)
    if not target.is_dir():
        raise HTTPException(404)
    with job_lock:
        if rid in job_queue:
            job_queue.remove(rid)
    shutil.rmtree(target)
    return {"deleted": True}


@app.get("/api/export/{rid}.md")
async def export(request: Request, rid: str):
    item = store(rid)
    names = item["speakers"]
    lines = [f"# {item['meta'].get('title', rid)}", ""]
    if item["summary"]:
        lines += [item["summary"], ""]
    lines += ["## Konuşma dökümü", ""]
    for turn in item["result"].get("turns", []):
        label = names.get(str(turn["speaker"]), f"Konuşmacı {turn['speaker']}")
        lines.append(f"- [{mmss(turn['start'])}] **{label}:** {turn['text']}")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/markdown; charset=utf-8")


if __name__ == "__main__":
    import uvicorn
    print(f"speech-studio: http://127.0.0.1:{STUDIO_PORT}/")
    uvicorn.run(app, host="127.0.0.1", port=STUDIO_PORT, log_level="warning")