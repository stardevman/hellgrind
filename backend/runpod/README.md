# MiniMax H3 · RunPod Serverless kurulumu

Hell Grind'in varsayılan video platformu. Modeller bir veri merkezindeki **Network Volume**'da durur; **Serverless uç nokta**
iş geldikçe worker açar (en az 0 worker), işler bitince boşta bekleme süresi dolunca kapanır. Ödeme saniye saniyedir:
worker'ın açılışı (ComfyUI ve model yükleme), işler ve boşta bekleme süresi faturalanır; imaj indirme faturalanmaz.

> Bu akış gerçek bir RunPod hesabında henüz çalıştırılmadı (sahte RunPod'la sınandı). İlk kez kendi gözetiminizde yapın.
> REST ve GraphQL alan adları RunPod belgelerinden ve OpenAPI'den alındı (Ekim 2026).

## Bölge (izinli liste)

MiniMax H3 lisansı AB, Birleşik Krallık, Güney Kore, ABD'yi ve bunların denizaşırı topraklarını kapsamaz. Hell Grind yalnızca
izinli veri merkezlerini kabul eder (`backend/app/core/regions.py`):

- Varsayılan: **CA-MTL-1, CA-MTL-2, CA-MTL-3** (Kanada, Montreal).
- İsteğe bağlı (Ayarlar › MiniMax H3 · RunPod › Kuzey veri merkezleri, varsayılan kapalı): **EUR-NO-1** (Norveç),
  **EUR-IS-1/2/3** (İzlanda).

Uç noktanın veri merkezi kısıtı RunPod API'sinden okunur. Kısıt yoksa ("her yerde") ya da izinli listenin dışında bir merkez
varsa, ya da kısıt okunamazsa üretim başlamaz.

## 1. Network Volume (bir kez)

RunPod konsolu › Storage › **New Network Volume**: veri merkezi **CA-MTL-1**, boyut **200 GB** (modeller ~105 GB + Pod'daki
ComfyUI ~10 GB + pay). Ücret 0,07 $/GB/ay (200 GB ≈ 14 $/ay). Volume kimliğini Ayarlar'a yazın (**Network Volume kimliği**).

## 2. Modelleri volume'a indirme (kurulum Pod'u, elle)

1. Volume'a bağlı geçici bir **Pod** açın (aynı veri merkezi; herhangi bir GPU ya da CPU Pod'u, ör. PyTorch şablonu).
   Volume Pod'da `/workspace`'e bağlanır.
2. Betiği Pod'a kopyalayıp çalıştırın:
   ```bash
   bash setup_volume.sh              # yalnızca modeller (paralel indirme)
   bash setup_volume.sh --comfyui    # + test ve iş akışı dışa aktarımı için ComfyUI (Pod'un 8188 portu)
   ```
   - İndirici: `hf download` (huggingface_hub, Xet; `HF_XET_HIGH_PERFORMANCE=1`) ya da `HG_DOWNLOADER=aria2c`
     (16 bağlantı). `HG_PARALLEL` dosya (varsayılan 4) aynı anda iner; yarım kalan yeniden çalıştırınca sürer.
   - Görsel modeli: `HG_IMAGE_MODEL=qwen-image-2512` (varsayılan, Apache 2.0) | `flux-schnell` | `none`.
   - Günlük: `/workspace/hellgrind/setup_volume.log`. Dosya başına süre ve hız, sonda toplam süre ve boyut yazılır.
3. `--comfyui` ile Pod'daki ComfyUI'de iş akışlarını deneyin ve **Workflow › Export (API)** ile kaydedin. Değişen değerleri
   yer tutucuyla değiştirip Ayarlar › ComfyUI iş akışları'ndan yükleyin (`t2v`, `i2v`, `r2v`, `keyframe`;
   `comfy-workflows/README.md`). Kaydetme düğümünde `{{filename_prefix}}` kullanın.
4. Bitince **Pod'u silin** (volume ve modeller kalır).

Volume yerleşimi: `models/diffusion_models`, `models/text_encoders`, `models/vae`, `models/loras`, `models/checkpoints`.

## 3. Worker imajı

`backend/runpod/worker/`:

- `Dockerfile`: `runpod/worker-comfyui:5.11.0-base` (RunPod'un resmî ComfyUI worker'ı, modelsiz). ComfyUI'yi H3 yerel
  düğümlerini içeren güncel sürüme getirir (`COMFYUI_REF` ile sabitlenebilir).
- `extra_model_paths.yaml`: `/runpod-volume/models/...` (diffusion_models ve text_encoders dahil; resmî dosyada yok).
- `handler.py`: resmî handler'ın yerine geçer. Videoyu da döndürür (resmî handler yalnızca `images` anahtarını toplar).
  Her dosyanın boyutunu ve SHA-256'sını, GPU adını ve soğuk başlatma süresini bildirir.

```bash
docker build --platform linux/amd64 -t <kullanıcı>/hellgrind-h3-worker:1 backend/runpod/worker
docker push <kullanıcı>/hellgrind-h3-worker:1
```

**Çıktı boyutu:** RunPod `/run` sonuçlarını ~10 MB'ta keser. Handler çıktıyı 9 MB'a kadar (base64) yanıtta döndürür.
Daha büyük klipler için uç noktanın ortamına S3 uyumlu bir kova ekleyin: `BUCKET_ENDPOINT_URL`, `BUCKET_ACCESS_KEY_ID`,
`BUCKET_SECRET_ACCESS_KEY`, `BUCKET_NAME` (ör. Cloudflare R2; kovaya 1 günlük silme kuralı koyun). Handler dosyayı yükler
ve 24 saatlik imzalı adres döndürür. Kova yoksa iş açık bir hatayla biter. CA-MTL-1 volume'ları RunPod'un S3 API'sini
desteklemez.

## 4. Serverless uç nokta

RunPod konsolu › Serverless › **New Endpoint** › özel imaj (`<kullanıcı>/hellgrind-h3-worker:1`):

- **GPU:** RTX 5090 (`ADA_32_PRO`) ve RTX PRO 6000 (`BLACKWELL_96`).
- **Veri merkezi:** yalnızca volume'unki (CA-MTL-1).
- **Network Volume:** 1. adımdaki.
- **Worker:** en az 0 (flex), en çok iş yükünüze göre (ör. 2–3).
- **Boşta bekleme:** 60 sn (Ayarlar › Boşta bekleme).
- **FlashBoot** açık.
- **Execution timeout:** en az 3600 sn (Hell Grind her işte `policy.executionTimeout` gönderir).

Uç nokta kimliğini ve API anahtarını Ayarlar › MiniMax H3 · RunPod'a yazın. **Ayarları uç noktaya uygula** düğmesi GPU
tiplerini, veri merkezini, volume'u, en az 0 worker'ı ve boşta bekleme süresini RunPod REST API v2 ile uç noktaya yazar
(`PATCH /v2/serverless/{id}`). Veri merkezi izinli değilse yazmaz. **Bağlantıyı sına** bakiyeyi, uç nokta ayarını (bölge
denetimi) ve worker/kuyruk sayılarını okur; worker başlatmaz.

## Fiyat ve bütçe

`pricing.yaml › serverless.runpod`: flex fiyatları (runpod.io/pricing, sayfa 2026-10-05): RTX 5090 **1,58 $/sa**
(0,000439 $/sn), RTX PRO 6000 **3,49 $/sa** (0,000969 $/sn). Fiyatları ücretli moda geçmeden önce RunPod'un sayfasından
yeniden doğrulayın.

- **Rezervasyon:** en pahalı GPU tipiyle, video saniyesi başına GPU süresi (`render_seconds`) × süre.
- **İşlenen tutar:** RunPod'un bildirdiği çalışma süresi (`executionTime`) ve worker'ın ilk işiyse soğuk başlatma süresi,
  işi çalıştıran GPU tipinin saniye ücretiyle.
- **Boşta bekleyen worker'lar:** dakikalık görev (`hg.runpod_tick`) `/health`'teki boşta worker sayısı × geçen süre × en
  pahalı tipin ücretini son etkin projeye yazar.
- **Bakiye koruması:** her yeni işten önce RunPod bakiyesi okunur (GraphQL `myself.clientBalance`, 60 sn önbellek). Bakiye
  **Bakiye eşiği**nin (varsayılan 10 $) altındaysa iş başlamaz; panelde uyarı görünür. Bakiye okunamazsa uyarı yazılır ama
  üretim durmaz. RunPod GraphQL'i 2027 başında kaldıracağını duyurdu.
- **Toplu gönderim:** bir dalganın bütün işleri (test turu ve tam tur dalgası) birlikte kuyruğa girer. Soğuk başlatma işler
  arasında paylaşılır. Önce bütün storyboard kareleri, sonra bütün videolar sırası korunur.

## Bilinen sınırlar

- Başarısız işlerin RunPod'daki süresi projeye yazılmaz (rezervasyon iade edilir); RunPod yine de faturalar.
- Sonuçlar tamamlandıktan 30 dk sonra RunPod'dan silinir. Worker çökerse yarım indirmeler bu süre içinde dakikalık görevle
  yeniden alınır; daha geç kalanlar Ayarlar › Çıktı güvencesi'nde "eksik" görünür.
- `runpod/worker-comfyui` taban imajının içi (`/comfyui` git deposu, `python -m pip`) RunPod'un deposundan okundu; derleme ilk
  kez yapılırken doğrulanmalı.
