# Kurulum — Speech Studio (Jetson, Nemotron-3-Diarization + NeMo-Speech.cpp)

Bu belge, ses kayıtlarından **konuşmacı ayrımı (diarization) + transkript + özet**
üreten sistemin kurulumunu adım adım anlatır. Hedef makine: NVIDIA Jetson
(Orin ailesi), Ubuntu 22.04, Docker + NVIDIA Container Toolkit. Doğrulanmış
tezgâh: **JetPack R36 (L4T), CUDA 12.2, 6 çekirdek, 7 GB RAM**.

Ses modelleri yerel GPU'da çalışır; sesler cihaz dışına **çıkmaz**. Ollama
konteyneri (LLM) ayrıdır ve kurulum boyunca **dokunulmaz**.

---

## 1. Gereksinimler

| Öğe | Not |
|---|---|
| Jetson Orin (Ampere, sm_87) | Orin Nano/NX/AGX |
| JetPack 6 / Ubuntu 22.04 | `cat /etc/nv_tegra_release` → `R36` |
| CUDA sürücüsü | `nvcc --version` veya `/usr/local/cuda/version.json` → 12.2 |
| Docker | `docker --version` (≥ 27 önerilir) |
| NVIDIA Container Toolkit | `docker info | grep -i runtime` → `nvidia` listelenmeli |
| ffmpeg | sesi 16 kHz mono WAV'a çevirir |
| Python 3.10 | web arayüzü servisi için |
| Disk | imaj + modeller için ≈ 15 GB boş alan |

Eksik paketler için `sudo apt install -y ffmpeg python3-venv`.

---

## 2. Proje düzeni

```
~/speech-studio/
  README.md
  docs/                      ← bu dosyalar
  server/                    ← web arayüzü (Python/FastAPI) + static/
  scripts/setup.sh           ← servis kurulumu
  scripts/nemo-run.sh        ← elle NeMo-Speech CLI denemeleri için sarmalayıcı
  model-cache/               ← GGUF model önbelleği (kalıcı)
  library/                   ← kayıtlar ve işlenmiş çıktılar
```

---

## 3. NeMo-Speech.cpp Docker imajı

Depo, Docker'ı **resmen** destekler (`docker/Dockerfile`, çok aşamalı; aarch64
dahil). Tegra platformları için kritik iki nokta vardır ve build argümanları
buna göre seçilir:

1. **CUDA araç zinciri = sürücü sürümü.** Tegra'da `cuda-compat` katmanı yoktur;
   sürücüden yeni araç zinciriyle derlenen kod yanlış sonuç üretebilir. JetPack
   R36 = CUDA **12.2** olduğundan imaj `12.2.0-devel-ubuntu22.04` ile derlenir
   (depo varsayılanını kullanmayın).
2. **GPU mimarisi = sm_87 (Orin).** `CUDA_ARCH=87` verilirse çekirdekler yerel
   SASS ile derlenir; boş bırakılırsa ggml taşıyabilir derleme (sm_80 PTX JIT)
   kullanır — çalışır ama yavaştır.

```bash
cd ~/speech-studio/vendor/NeMo-Speech.cpp
git submodule update --init --recursive --depth 1

docker build --progress=plain -f docker/Dockerfile --target runtime \
  --build-arg CUDA_VERSION=12.2.0 \
  --build-arg UBUNTU_VERSION=22.04 \
  --build-arg CUDA_ARCH=87 \
  --build-arg ENABLE_GRPC=OFF \
  --build-arg ENABLE_HTTP=OFF \
  --build-arg ENABLE_NMT=OFF \
  --build-arg ENABLE_S2S=OFF \
  --build-arg ENABLE_TTS_JA=OFF \
  --build-arg ENABLE_TTS_ZH=OFF \
  --build-arg JOBS=4 \
  -t speech-nemo:local .
```

- **Hedef `runtime`:** model içermeyen, yalnız gerekli kütüphaneleri toplayan
  ince kök dosya sistemi (`FROM scratch`). `libcuda.so.1` çalışma anında NVIDIA
  Container Toolkit tarafından enjekte edilir.
- **`JOBS=4`:** 7 GB RAM makinede derleme sırasında OOM olmaması için. Azı 2.
- **Kapatılanlar** (gRPC/HTTP sunucu, NMT, VoiceChat, TTS JA/ZH) bu iş akışında
  kullanılmaz; derlemeyi ciddi biçimde kısaltır. `ENABLE_FLASHLIGHT=ON` ve
  `ENABLE_NORM=ON` (varsayılan) kalır — CTC kelime güçlendirme + ITN transkript
  kalitesini artırır.

### Doğrulama

```bash
docker image inspect speech-nemo:local >/dev/null && echo hazır
scripts/nemo-run.sh --help | head      # CLI imzaları görünmeli
```

> **Jammy uyarlaması (gerekli):** Dockerfile varsayılan tabanı Ubuntu 24.04'tür;
> 24.04 için CUDA 12.2 imajı bulunmadığından (Tegra sürücü eşleşmesi için 22.04
> zorunlu) `gcc-13`/`g++-13` paketleri jammy deposunda yoktur. İlk derlemede
> `E: Unable to locate package gcc-13` görülür; `docker/Dockerfile` içindeki
> `gcc-13`/`g++-13` satırları `gcc-12`/`g++-12` ile değiştirilmelidir
> (kalıp değiştirme, Orijinal Dockerfile `Dockerfile.orig` olarak saklanmıştır).
> Kalan yapı aynıdır: ITN zaten gcc-12 ile derlenir; C++17 yeterlidir.
>
> **cuBLAS (Tegra):** CUDA 12.2 arm64 imajları *SBSA* (sunucu) cuBLAS'ı taşır;
> Tegra'da `cublasCreate_v2` "the resource allocation failed" ile çöker ve
> varsayılan ggml-cuda sembollerinin tamamı gerçek cuBLAS'ı ister
> (`cublasSetWorkspace_v2`). Bu yüzden kurulum, JetPack'in kendi
> `libcublas.so.12` + `libcublasLt.so.12` dosyalarını `/usr/local/cuda/lib64`'ten
> gerekmeyebilir.

> **Bilinen uç durum:** `nvcr.io/nvidia/cuda:12.2.0-devel-ubuntu22.04` indirilemezse
> (manifest yok) Docker Hub eşini çekip adlandırın:
> `docker pull nvidia/cuda:12.2.0-devel-ubuntu22.04 && docker tag nvidia/cuda:12.2.0-devel-ubuntu22.04 nvcr.io/nvidia/cuda:12.2.0-devel-ubuntu22.04`
> — Dockerfile değişmez, build aynı komutla devam eder.

---

## 4. Modellerin kurulumu

NeMo-Speech.cpp modelleri GGUF biçiminde Hugging Face'ten çekip **SHA-256 ile
doğrular**. İndirme adresleri ve özetler `models/index.json` içinde sabittir;
CLI ilk kullanımda gerekli olanı otomatik indirir.


```
Linux:  $XDG_CACHE_HOME/nemo-speech/models  ≈  ~/speech-studio/model-cache/nemo-speech/models
```

Önceden indirmek için (ağ kapalı kalmadan önce):

```bash
scripts/nemo-run.sh pull nemotron-3.5          # ASR (varsayılan, q8_0, ~710 MB)
scripts/nemo-run.sh pull nemotron-3-diar       # diarizasyon (varsayılan) 
```

`--diarize` verildiğinde diarizasyon modeli gerekliyse otomatik iner.
Ayrıntılar ve alternatif ASR modelleri için: [MODELLER.md](MODELLER.md).

---

## 5. Web servisi

```bash
cd ~/speech-studio
bash scripts/setup.sh
```

Yaptıkları: `.venv` + `fastapi/uvicorn` kurulumu, `systemctl --user` birimi
üretimi. Çıktıda erişim adresi ve tünel komutu verilir.

Başka bilgisayardan erişim:

```bash
ssh -N -L 17841:127.0.0.1:17841 <jetson-kullanıcı@jetson>
```

Değişkenler (`setup.sh` öncesi ortama verilebilir):

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `SPEECH_STUDIO_ROOT` | `~/speech-studio` | veri/proje kökü |
| `SPEECH_STUDIO_PORT` | `17841` | loopback portu |
| `SPEECH_NEMO_IMAGE` | `speech-nemo:local` | NeMo-Speech imajı |
| `STUDIO_LLM_MODEL` | `qwen2.5:3b` | özet/soru modeli (Ollama) |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama uç noktası (yalnız loopback) |

---

## 6. Ollama tarafı (değiştirilmez)

Özet ve soru-cevap, mevcut Ollama konteynerinizdeki modele `127.0.0.1:11434`
üzerinden gider. Kurulum **asla** konteyneri başlatmaz/durdurmaz/silmez;
ulaşılamazsa yalnız özet/soru düğmeleri uyarı verir, kayıtlar gene işlenir.

```bash
curl -s http://127.0.0.1:11434/api/tags | python3 -m json.tool | grep '"name"'
```

Başka bir model seçmek için: `STUDIO_LLM_MODEL=qwen3:4b bash scripts/setup.sh`.
Not: modelin Turkish özet kalitesini doğrulayın; küçük modeller kısa tutarlı
özet üretir.

---

## 7. Doğrulama akışı

1. **CLI duman testi** (model indirmesini de doğrular):

```bash
scripts/nemo-run.sh transcribe ~/speech-studio/build/samples/diarization_example.mp3 \
  --diarize --json | head -c 600
```

`words` dizisinde her kelime için `speaker` alanı (1-tabanlı) görünmeli.

2. **Uçtan uca:** tarayıcıdan örnek sesi yükleyin → durum `processing` → `done`;
   konuşma turu renklendirmesi, döküm, "Özet üret", "Kayda sor" akışlarını
   yoklayın.

3. **Servis sağlığı:** `systemctl --user status speech-studio` ve
   http://127.0.0.1:17841/api/status`.

---

## 8. Sorun giderme

| Belirti | Sebep / çözüm |
|---|---|
| `docker build` OOM ile ölür | `JOBS=2` ile yeniden derleyin; biten kopyalar cache'ten devam eder |
| `manifest unknown` (nvcr) | Bölüm 3'teki Docker Hub ad etiketleme köprüsü |
| `exec format error` | İmaj yanlış mimariyle kurulmuş; `docker image inspect` → `Architecture: arm64` olmalı |
| `could not select device driver with capabilities: [[gpu]]` | NVIDIA Container Toolkit eksik; `nvidia-ctk` kurulumu, ardından `sudo systemctl restart docker` |
| Model inmiyor, `HTTP 4xx/5xx` | HF erişimini/proxy'yi kontrol edin; `scripts/nemo-run.sh model list` durumu gösterir |
| Dosya işlenmiyor, `Model kelimeli çıktı üretmedi` | Ses çok sessiz/bozuk ya da desteklenmeyen kodlama; ffmpeg ile 16 kHz WAV'a çevrilmiş olmalı (uygulama çevirir) |
| "Özet alınamadı: Model sunucusuna ulaşılamadı" | Ollama kapalı; `docker start <ollama-konteynır>` sizin yetkinizdedir (sistem yapmaz) |
| Port dolu | `SPEECH_STUDIO_PORT=17850 bash scripts/setup.sh` |
| İşlem 10 dk'dan uzun | Kayıt çok uzun olabilir; `library/<id>/job.json` → `detail` alanına bakın; konteyner `timeout=3600` ile sınırlandırılmıştır |

---

## 9. Güncelleme ve taşıma

- **NeMo-Speech.cpp güncelleme:** `cd vendor/NeMo-Speech.cpp && git pull &&
  git submodule update --init --recursive && docker build ...` (Bölüm 3).
  Yeni model çıktısında `server/app.py` içindeki `parse_transcription` alan
  adları uyumludur (kelime listesi esnek okunur).
- **Yedekleme:** `library/` insan-okunurdur (`meta.json`, `result.json`,
  `summary.md`, `questions.json`, ses dosyaları). Olduğu gibi kopyalanır.
- **Kaldırma:** `systemctl --user disable --now speech-studio` + `docker rmi
  speech-nemo:local`. Modeller `model-cache/` altında kalır (isteyen siler).

## 10. Lisans ve gizlilik

- Nemotron-3-Diarization: NVIDIA Open Model License (OpenMDW 1.1) — ticari
  kullanım serbest.
- NeMo-Speech.cpp: Apache-2.0. GGUF dosyaları hem lisans metniyle hem de
  indirme sırasında SHA-256 ile doğrulanır.
- Ses kayıtları, transkript ve özet yalnız `~/speech-studio/library` altında
  durur; arayüz loopback'tir. Model çağrıları da loopback üzerindendir
  (Ollama `127.0.0.1`, NeMo konteyneri yerel).