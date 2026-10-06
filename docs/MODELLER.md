# Modeller — nemotron-3-Diarization, ASR ve LLM rolleri

Speech Studio üç farklı rol için üç farklı yerel çalıştırıcı kullanır.
Aşağıda her modelin **ne işe yaradığı, girdi/çıktısı, biçimi ve donanım notları**
bulunur. Kurulum adımları için [KURULUM.md](KURULUM.md).

| Rol | Model | Çalıştırıcı | Cihaz |
|---|---|---|---|
| Kim-ne-zaman-konuştu (diarization) | `nvidia/Nemotron-3-Diarization` | NeMo-Speech.cpp (ggml/GGUF) | Jetson GPU |
| Konuşma → metin (ASR) | `nvidia/nemotron-3.5-asr-streaming-0.6b` (varsayılan) | NeMo-Speech.cpp | Jetson GPU |
| Özet + soru-cevap | `qwen2.5:3b` (ya da seçtiğiniz) | Ollama konteyneri | Jetson GPU |

---

## 1. nemotron-3-Diarization ("ses ayırt etme")

**Ne yapar:** Kayıtta *kim ne zaman konuştu* sorusunu çözer; 8 kişiye kadar
konuşmacıyı sırayla etiketler (konuşmacı kanalları konuşmaya giriş sırasına
göre dizilir — "Konuşmacı 1" ilk konuşandır). 100M parametrelik Sortformer
tabanlı bir dönüştürücü; 10 ms Mel özellikleri 80 ms kare hızına indirger.

**Girdi:** 16 kHz, tek kanal ses — `.wav`, `.flac`, `.opus`, `.mp3`.
Süre sınırı yoktur (parçalı çıkarım). Uygulama yükleme sırasında her dosyayı
`ffmpeg` ile 16 kHz mono PCM WAV'a çevirerek bu şartı garantiye alır.

**Çıktı:**
- **Segmentler:** `(başlangıç_saniye, bitiş_saniye, konuşmacı_no)` — zaman
  çizelgesi bunlarla boyanır.
- **Kelime düzeyi konuşmacı etiketi** (bizim iş akışımız): `transcribe --diarize
  --json` çıktısında her kelime 1-tabanlı `speaker` alanı taşır; döküm turları
  buradan üretilir.

**Model kartı gerçekleri:**
- Sürümler (80 ms kareler, gecikme = CHUNK_LEN+RIGHT_CONTEXT × 80 ms):

  | Yapı | Gecikme | Not |
  |---|---|---|
  | Offline | 30.4 s | en iyi kalite (kayıt dosyaları için önerilen) |
  | Low | 1.04 s | canlı akış |
  | Very low | 0.64 s | canlı akış |
  | Ultra low | 0.32 s | canlı akış |

  NeMo-Speech.cpp `diarize` yolu offline tarzda çalışır; canlı akış gerekiyorsa
  `nemo-speech transcribe --live --diarize` desteklenir (arayüzümüz şimdilik
  dosya tabanlıdır).
- **Kalite (kıyas):** DIHARD III'de 12.73 DER (4 konuşmacıya kadar 9.13),
  CALLHOME'da 9.10 — önceki nesil streaming Sortformer 4spk-v2.1'e göre
  ciddi iyileşme. (DER = diarization hata oranı; düşük iyi.)
- **Lisans:** NVIDIA Open Model License (OpenMDW 1.1) — ticari/karışıksız kullanım.

**Biçim:** NeMo-Speech.cpp için GGUF (q8_0) gerekir. HF deposunda GGUF hazır
gelmektedir; NeMo (`SortformerEncLabelModel`) ya da 🤗 Transformers checkpoint'i
`convert_model.py` ile GGUF'a çevrilebilir [NeMo-Speech.cpp model conversion](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/main/docs/model-conversion.md).

---

## 2. ASR (söylenenleri metne çeviren)

**Varsayılan:** `nvidia/nemotron-3.5-asr-streaming-0.6b`
(kısa ad `nemotron-3.5`, GGUF q8_0, **741.548.352 bayt**, SHA-256 kayıtlı;
indirme sırasında doğrulanır). Akışlı çalışır (160 ms dilimler) ama kayıt
dosyalarında da kullanılır; sözcük zaman damgaları üretir — diarizasyonla
birleştirilince her zaman damgası konuşmacı etiketi alır.

**Alternatifler** (CLI `--model` ile seçilebilir; indirme otomatik):

| Kısa ad | Model | Boyut (q8_0) | Not |
|---|---|---|---|
| `parakeet-tdt` | `nvidia/parakeet-tdt-0.6b-v3` | — | TDT kalitesi, offline odaklı |
| `parakeet-ctc` | `nvidia/parakeet-ctc-1.1b` | 1.178.100.960 B | CTC + dil modeli güçlendirme (Flashlight) |
| `nemotron-en` | `nvidia/nemotron-speech-streaming-en-0.6b` | 699.872.960 B | İngilizce–özel, daha küçük |

(Boyutlar `vendor/NeMo-Speech.cpp/models/index.json` içindeki sabit
`size`/`sha256` değerleridir; değişmişse depo sürümünüze bakın.)

**Seçim önerisi:** Sonuç kalitesi arıyorsanız deneme için bile `nemotron-3.5`
varsayılanı iyi dengedir; İngilizce yoğun kayıtlarda `parakeet-tdt` kıyaslanmalı.
Araç çalışmanı ana motoruz: hepsi aynı `transcribe --diarize --json`
komutundan geçer; sesiniz Türkçe/çok dilli olduğunda dil desteği ayrıca
filtrelenmelidir (nemotron-3.5 çok dilli eğitimli, model kartına bakınız).

---

## 3. GGUF indirme ve doğrulama

```bash
scripts/nemo-run.sh model list      # mevcutlar, kısa adlar, hangi komutda
scripts/nemo-run.sh pull nemotron-3.5
```

- İndirme yolu: `~/speech-studio/model-cache/nemo-speech/models/`
  (konteynerde `/modelhome/.cache`).
- CLI her artefact'ı sabit boyut + SHA-256 ile doğrular; uyumsuzsa indirmeyi
  yeniler.
- Ağdan bağımsız çalışma için indirmeyi kurulum anında yapın; sonraki işler
  tamamen yerel yürür.

Dönüşüm (özel checkpoint → GGUF): [model-conversion.md](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/main/docs/model-conversion.md).
`.nemo` (NeMo) ve HF Transformers ağırlık biçimleri desteklenir.

---

## 4. Donanım notları (Jetson)

- **Ampere sm_87** (Orin) mimarisi; ggml-CUDA çekirdekleri `CUDA_ARCH=87`
  ile yerel derlenir (bkz. KURULUM.md Bölüm 3).
- **CUDA araç zinciri = sürücü sürümü zorunlu:** Tegra'da cuda-compat yoktur;
  12.2 sürücüyle 13.x toolkit kodu yanlış hesap yapabilir. İmajı 12.2 ile
  derleyin.
- **RAM:** iki model toplamda < 2 GB VRAM/RAM konforla sığar; 7 GB makinede
  Ollama açıkken bile rahattır. Derleme alanı JOBS=4 ile güvenlidir.
- **Hız referansları** (NeMo-Speech.cpp BENCHMARK): ASR CPU'da 6× gerçek
  zaman, modern GPU'larda 67×'ye kadar — Orin'de ASR tahmini birkaç yüz ms
  dilim/dakika; offline kayıt işleme kayıt başına saniyeler sürer.
  Diarizasyon offline RTFx model kartında 1340× (batch 1, compiled) — kayıt
  başına pratikte saniyeler.

---

## 5. LLM tarafı (özet/soru) — Ollama

Bu rol NeMo-Speech.cpp'nin işi değildir; **mevcut Ollama konteyneriniz**in
HTTP API'sine bağlıdır:

- Uç nokta: `http://127.0.0.1:11434` (yalnız loopback; arayüzden gelen çağrı
  контeyner değiştirmez, başlatmaz, model indirmez).
- Varsayılan: `qwen2.5:3b`. Değiştirmek için `STUDIO_LLM_MODEL`.
- İstemci çağrıları `/api/chat`, `stream:false`, `num_ctx 4096`;
  özet için `num_predict 700`, sorularda aynı sistem yönergeleriyle
  transkript parçaları verilir ve kaynak zamanı istenir (kanıta dayalı
  davranış — uydurma cevap vermemesi için sistem talimatı kısıtlıdır).

Mevcut kurulumunuzdaki modeller (dokunulmamıştır): `qwen2.5:3b`,
`qwen3:4b`, `qwen3:1.7b`, `openbmb/minicpm5-2b`.

---

## 6. Lisans/etik özeti

| Varlık | Lisans | Not |
|---|---|---|
| Nemotron-3-Diarization | OpenMDW 1.1 | ticari kullanım açık |
| nemotron-3.5 ASR / Parakeet | OpenMDW 1.1 / CC-BY-4.0 | model bazında farklıdır |
| NeMo-Speech.cpp | Apache-2.0 | |
| ggml/llama.cpp | MIT | gömülü alt modül |

Ses kayıtlarınız kişisel veri içerebilir; sistem ses verisini yalnız yerelde
işler (konteyner ve LLM çağrıları dâhil).