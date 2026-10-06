# Speech Studio

Ses kayıtları üzerinden çalışan yerel çalışma masası: **kim-ne-zaman-konuştu**
(konuşmacı renkli zaman çizelgesi), **konuşmacı etiketli transkript**, **özet**
ve **kayda soru sorma**.

- Ses ayrımı + transkript: [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp)
  konteyneri — [Nemotron-3-Diarization](https://huggingface.co/nvidia/Nemotron-3-Diarization)
  + yerel ASR GGUF modelleri (Jetson GPU'sunda).
- Özet ve sorular: mevcut Ollama konteynerinizdeki model (varsayılan `qwen2.5:3b`).
- Arayüz: tarayıcı, yalnız 127.0.0.1 → SSH tüneliyle erişilir.

## Hızlı başlangıç

```bash
cd ~/speech-studio
bash scripts/setup.sh
# adres + anahtar çıktısı verilir, örn.:
ssh -N -L 17841:127.0.0.1:17841 <jetson>
# tarayıcıda: http://localhost:17841/?key=<anahtar>
```

Ses dosyasını sürükleyip bırakın; işlem bitince konuşma turu renkleri,
döküm ve özet görünür. İsimlendirmeler (Konuşmacı 1 → "Ayşe") üzerine
tıklayarak değiştirilir.

## Belgeler

- [docs/KURULUM.md](docs/KURULUM.md) — kurulum, model indirme, sorun giderme
- [docs/MODELLER.md](docs/MODELLER.md) — modellerin tanımı, biçimleri, donanım notları

## Notlar

- Tüm veri `~/speech-studio/library/<id>/` altındadır; kopyalanabilir, silinebilir.
- NeMo konteyneri iş başına (`docker run --rm`) çalışır; kurulum kalıcı servis
  yalnız web arayüzü içindir. Hiçbir adımda Ollama konteynerine dokunulmaz.
- Arka uç: `server/app.py` (Python/FastAPI). Elle CLI denemeleri için
  `scripts/nemo-run.sh`.