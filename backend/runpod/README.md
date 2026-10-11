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
3. `--comfyui` ile Pod'daki ComfyUI'de (0.39.0) iş akışlarını deneyin. `i2v`, `r2v` ve `keyframe` hazırdır
   (`comfy-workflows/`, resmî şablonlardan `exported/make_templates.py` ile). Yeniden dışa aktarırsanız (**Workflow › Export
   (API)**) dosyaları `comfy-workflows/exported/`'a koyup üreteci çalıştırın.
4. Bitince **Pod'u silin** (volume ve modeller kalır).

Volume yerleşimi (ComfyUI'nin klasör adları): `models/unet`, `models/clip`, `models/vae`, `models/loras` (+ `checkpoints`).
Video VAE `minimax_h3_video_vae_fp16` (iş akışları bunu bekler); `*_pruned_fp8_scaled` DiT'ler int8_convrot'un yedeğidir.

> **EUR-NO-1 volume'u:** Kanada dışındaki bir volume'da (ör. EUR-NO-1) Ayarlar › **Kuzey veri merkezleri**'ni açın ve
> **Veri merkezi**'ni `EUR-NO-1` yapın; yoksa bölge denetimi üretimi başlatmaz. EUR-NO-1, RunPod'un S3 API'sini destekler.

## 3. Worker imajı

`backend/runpod/worker/`:

- `Dockerfile`: `runpod/worker-comfyui:5.10.0-base` (RunPod'un resmî ComfyUI worker'ı, modelsiz; Docker Hub'daki en yeni
  yayımlanmış etiket, CUDA 12.8.1, torch 2.11.0+cu128). İçindeki ComfyUI 0.34.0, **0.39.0** sürüm arşiviyle değiştirilir
  (`COMFYUI_REF=v0.39.0`): `comfy-workflows/` altındaki iş akışları bu sürümün resmî şablonlarından dışa aktarıldı, düğüm ve
  girdi adları ona göredir (özel düğüm gerekmez). Derleme, H3 düğümleri yoksa durur ve ComfyUI'yi CPU'da bir kez açıp
  bütün düğümleri yükler (açılışı bozan bağımlılık derlemede yakalanır). torch yeniden kurulmaz.
- **H3 DiT türü:** Comfy-Org int8_convrot DiT'leri cu130 torch'la, fp8_scaled'ı cu130 olmazsa önerir. İmaj cu128'dir:
  iş akışları `minimax_h3_{fl2va,ref2va}_pruned_{{unet_variant}}` yükler, uygulama bunu **Ayarlar › H3 DiT türü**'nden
  (`H3_UNET_VARIANT`, varsayılan `fp8_scaled`) doldurur ve klip meta verisine yazar. Derleme, torch'un CUDA etiketi seçilen
  türe uymazsa durur (`--build-arg H3_UNET_VARIANT`; cu128 → fp8_scaled, cu130 → int8_convrot). Worker da cu128'de
  int8_convrot iş akışını GPU'ya göndermeden reddeder. Metin kodlayıcı nvfp4_awq (Blackwell şart değil), video VAE fp16.
- `extra_model_paths.yaml`: `/runpod-volume/models/{unet,clip,vae,loras}` (diffusion_models ve text_encoders adlarıyla da).
- `handler.py`: resmî handler'ın yerine geçer. Videoyu da döndürür (resmî handler yalnızca `images` anahtarını toplar).
  Her dosyanın boyutunu ve SHA-256'sını, GPU adını ve soğuk başlatma süresini bildirir.

### İmajı RunPod'a ulaştırma: bir kayda derleyip gönderme (önerilen)

RunPod uç noktası imajı herkese açık ya da kimlik bilgisi tanımlı bir kayıttan çeker. İmajda gizli bilgi yoktur (handler,
model yolları); herkese açık bir Docker Hub deposu yeterlidir.

1. Docker Hub'da (hub.docker.com) bir depo açın: `hellgrind-h3-worker` (Public). Bu Mac'te: `docker login`.
2. Depo kökünden derleyip gönderin (linux/amd64; Apple Silicon'da öykünmeyle, ilk seferde taban imaj ~15 GB iner):
   ```bash
   backend/runpod/worker/push.sh docker.io/<docker-hub-kullanıcı>/hellgrind-h3-worker 0.39.0-1
   ```
   Etiket `<ComfyUI sürümü>-<düzeltme>`: `handler.py`, `extra_model_paths.yaml` ya da `Dockerfile` değişince numarayı
   artırın (`0.39.0-2`). `latest` kullanmayın: worker'lar imajı önbelleğe alır, aynı etiketle yeni imaj gelmeyebilir.
3. RunPod konsolu › Serverless › uç nokta › **Container Image**: `docker.io/<docker-hub-kullanıcı>/hellgrind-h3-worker:0.39.0-1`
   (yeni etiket yazıp kaydedince worker'lar yeni imajla açılır). Depoyu gizli tutarsanız RunPod › Settings › **Container
   Registry Auth**'a Docker Hub erişim belirteci ekleyip uç noktada seçin.

**GitHub Actions'la (önerilen; derleme yerel ağa ve öykünmeye takılmaz):** `.github/workflows/worker-image.yml`
aynı derlemeyi ubuntu-latest'te (linux/amd64) yapar ve `docker.io/stardevman/hellgrind-h3-worker:<etiket>`'e gönderir
(sırlar: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`). Başlatmak için ya Actions › worker-image › **Run workflow** (etiket ve DiT
türü; iş akışı varsayılan dalda olmalı) ya da bir etiket gönderin:
```bash
git tag worker-v0.39.0-1 && git push origin worker-v0.39.0-1
```
Bitince işin özetinde RunPod'a yazılacak Container Image görünür.

GHCR da olur (`ghcr.io/<kullanıcı>/hellgrind-h3-worker:0.39.0-1`, `docker login ghcr.io`), ama taban imajın katmanları da
oraya yüklenir (~15 GB). Docker Hub'da taban katmanlar genellikle aynı kayıttaki `runpod/worker-comfyui`'den bağlanır,
yüklenmez; yine de ilk gönderim birkaç GB sürebilir.

**Alternatif: RunPod'un GitHub'dan derlemesi.** Uç noktayı GitHub deposuna bağlayıp Dockerfile yolunu
(`backend/runpod/worker/Dockerfile`) verirsiniz; RunPod derler. Kısıtlar (docs.runpod.io › GitHub integration): `docker
build` en çok 30 dk, bütün süreç 160 dk, imaj en çok 80 GB; depodaki değişiklik kendiliğinden gitmez, her güncelleme için
GitHub'da yeni bir **release** gerekir; derleme bağlamı olarak hangi klasörün kullanıldığı belgede yazmıyor (Dockerfile'ımız
`backend/runpod/worker` bağlamını bekler). Bu yol için depo GitHub'a gönderilmiş olmalı.

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

- **Ölçüm (2026-10-08, RTX 5090, fp8_scaled, turbo, 480p, 4 sn klip = 107 kare; faturalanan):** FL2VA 42-44 sn/klip
  (≈ 0,018-0,019 $), Ref2VA ~27 sn/klip (≈ 0,012 $), soğuk worker'da ilk işe ~140 sn eklenir (≈ +0,06 $), storyboard karesi
  sıcak 38-42 sn, soğuk ~139 sn. Duvar saati süreleri (175-215 sn) kuyruk ve imaj indirmeyi de içerir; faturalanan değildir.
- **Rezervasyon:** birincil GPU tipinin (Ayarlar'daki ilk GPU, RTX 5090) ücretiyle, H3 koşuluna göre video saniyesi başına
  GPU süresi (`render_seconds.fl2va` 11, `ref2va` 7; 720p şimdilik ×2) × üretilen süre (en az 4 sn). Worker soğuksa
  (az önce biten iş yok, /health'te açık worker yok) dalgadaki işlerden yalnızca biri soğuk başlatmayı da rezerve eder.
- **İşlenen tutar:** RunPod'un bildirdiği çalışma süresi (`executionTime`) ve worker'ın ilk işiyse soğuk başlatma süresi,
  işi çalıştıran GPU tipinin saniye ücretiyle.
- **Boşta bekleyen worker'lar:** dakikalık görev (`hg.runpod_tick`) `/health`'teki boşta worker sayısı × geçen süre × son
  işi çalıştıran GPU'nun ücretini son etkin projeye yazar. Bir H3 işi sürerken yazılmaz (o süre işin bedelinde, soğuk
  başlatma dahil): çift sayım yok. Geçmişte satırın süresi gerçek boşta saniyedir.
- **Bakiye koruması:** her yeni işten önce RunPod bakiyesi okunur (GraphQL `myself.clientBalance`, 60 sn önbellek). Bakiye
  **Bakiye eşiği**nin (varsayılan 10 $) altındaysa iş başlamaz; panelde uyarı görünür. Bakiye okunamazsa uyarı yazılır ama
  üretim durmaz. RunPod GraphQL'i 2027 başında kaldıracağını duyurdu.
- **Toplu gönderim:** bir dalganın bütün işleri (test turu ve tam tur dalgası) birlikte kuyruğa girer. Birden çok çekim
  birlikte işlenirken kuyruk model türüne göre gruplanır: önce metinden çizilen kareler (Qwen-Image), sonra karakterli
  kareler (Qwen-Image-Edit-2511), sonra bütün FL2VA/T2V videoları, sonra bütün Ref2VA videoları (her model değişimi
  20-40 GB'ı yeniden yükler, ~2-3 dk). Olay akışına "Model yükleniyor (soğuk başlatma)" ve "Model değişti" süreleriyle
  yazılır.
- **H3 koşulu:** her çekim varsayılan olarak FL2VA'dır. Karakterli çekimin ilk karesi Qwen-Image-Edit-2511 ile karakterin
  onaylı referansından çizilir (`edit_keyframe.json`; `KEYFRAME_CHARACTER_PROVIDER=qwen_edit`), H3 tuvalinde (1280x704).
  Kare kalite kapısından ve yüz kimliğinden (SFace ≥ `identity_threshold`) geçer; en çok 2 yeniden deneme, geçemezse "Kare
  inceleme gerekiyor" ve video yok. Ref2VA çekim bazında elle seçilir; Ref2VA çekiminde RunPod'a kare işi gönderilmez
  (storyboard için yerel kare; ücretli kare yalnızca `POST /scenes/{id}/board/draw?paid=true` ile). Karaktersiz çekimin
  karesi Qwen-Image ile metinden (en çok 1 kare). fal.ai kancası: `H3_KEYFRAME_REFERENCE=flux_kontext` (varsayılan kapalı).
- **Yinelenen gönderim yok (2026-10-09 olayından):** gönderilen her iş Redis'te iş anahtarıyla (seed, istem, kare,
  boyut) kayıtlıdır; worker süreci öldürülüp görev yeniden teslim edilirse yeni deneme aynı işi beklemeyi sürdürür,
  yenisini göndermez. Durum sorgusu okunamazsa (404: RunPod işi tanımıyor — kuyruk boşaltıldı ya da sonuç silindi —,
  zaman aşımı, 5xx) iş "durum bilinmiyor" olarak beklenir ve **yeniden gönderilmez**; 12 sorgudan (~1 dk) sonra çekim hata
  notuyla durur. Bir projede aynı anda en çok `RUNPOD_MAX_PENDING_JOBS` (6) iş bekler; fazlası yer açılana dek gönderilmez
  ("RunPod kuyruğu dolu" olayı). **Kill-Switch** projenin RunPod'daki bekleyen işlerini `POST /cancel/{id}` ile iptal eder;
  bekleyen görevler durur, rezervasyonlar iade edilir. Aynı görev 3 kez yarıda kesilirse (süreç her seferinde ölüyorsa)
  bir daha çalışmaz, çekim açıklamayla `failed` olur; yeniden teslimde önceki denemenin açık rezervasyonları iade edilir.
  Süpürme RunPod'un tanımadığı işleri `lost` olarak kapatır.
- **Bellek:** kalite skorlayıcısı (OpenCLIP + SFace) süreç başına ~3,6 GB'tır. Üretim kuyruğu (`gen`) bu yüzden ayrı bir
  worker'da, tek süreçte iş parçacığı havuzuyla çalışır (`worker-gen`: `--pool threads -c 4`); süreç havuzunda her alt
  süreç kendi kopyasını yüklüyor, 7,6 GB'lık Docker VM'inde süreçler SIGKILL ile ölüyordu.
- **Qwen-Image-Edit karesi:** `keyframe_edit_seconds` 195 sn (tam: 40 adım, cfg 4; ölçülen ~190-200 sn, ≈0,085 $),
  `keyframe_edit_medium_seconds` 100 (20 adım) ve `keyframe_edit_lightning_seconds` 15 (Lightning; ölçülen model yükleme dahil 0,048 $) başlangıç tahmini;
  soğukta Qwen-Image'ın soğuk/sıcak farkı eklenir. Kalite: Ayarlar › `QWEN_EDIT_MODE` ya da proje ayarı `qwen_edit_mode`. Volume'da: `models/unet/qwen_image_edit_2511_fp8mixed`
  (20 533 762 817 bayt), `models/loras/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16` (849 608 296 bayt); isteğe bağlı
  `models/loras/qwen-image-edit-2511-multiple-angles-lora.safetensors` (kesmede açı değişimi; elle yüklenir, sonra
  Ayarlar › `QWEN_EDIT_ANGLES=on`; yüklenmemişken düğüm istekten çıkarılır); metin
  kodlayıcı ve VAE Qwen-Image ile ortak. fp8mixed cu128 worker'da çalışır (int8_convrot cu130 ister). Worker imajı değişmez.
- **Kimlik kapısı:** yüz beklenen çekimde (insan karakter varsa; sırt çekimi, silüet, aşırı geniş ya da geniş aksiyon
  planında hayır; denetçiden değiştirilir) yüzü hiç görünmeyen ya da 0-1 kimlik skoru (SFace kosinüsü, aynı kişi eşiği
  0,363 çevresinde lojistik eğri; ham kosinüs değil) `identity_threshold`'un (0,60) altında kalan aday rafine havuza
  giremez. Kısa kenarı 80 px'ten küçük yüzde kimlik "ölçülemedi" sayılır, eleme yok. Referans olarak en net görsel önce
  (durağan görsel, ifade sayfası, sonra dönüş kareleri; küçük kareler büyütülür), en yüksek skor alınır. İnsan olmayan
  karakterde (`characters.kind = non_human`; Varlıklar'dan seçilir, tarifte robot/hayvan/yaratık geçiyorsa önerilir) yüz
  aranmaz: OpenCLIP görüntü benzerliği `look_threshold`'la (0,75) ölçülür. Hepsi kalırsa çekim "Kimlik düşük" gerekçesiyle
  incelemeye düşer; kontrolü geçemeyen ilk kare denetçiden elle onaylanabilir.
- **Anahtar kare zinciri ve FL2VA last_frame:** aynı sahnede devam eden çekimin ilk karesi önceki çekimin karesinden
  (Qwen-Image-Edit, image 1 = K(n-1)) sekans başına bir kilitle sırayla çizilir; `CUT TO:` sonrası kesme, yeni sahne
  başlığı zinciri kırar. i2v.json'a isteğe bağlı `{{last_frame}}` (LoadImage -> MiniMaxH3ImageToVideo.last_frame) eklendi;
  sonraki çekim devam ediyorsa video iki kare arasında üretilir, değilse düğüm istekten çıkarılır. Worker imajı değişmez
  (iş akışı istekle gider); ComfyUI 0.39.0'daki düğümün last_frame girdisi ilk ücretli işte doğrulanacak.
- **Referanslar:** karakter ve mekân referansı RunPod'da Qwen-Image-2512 ile durağan görsel olarak üretilir (keyframe.json;
  karakter 1328x1328, düz açık gri arka plan, tam boy, yüz net; mekân 1664x928). Karakterin dönüş videosu (H3) isteğe bağlı
  ikinci adımdır ve durağan görselden başlar. Qwen-Image-Edit'e image 1 olarak bu durağan görsel gider. Yeni referans
  eskisinin yerine geçince eskisi `assets.reference_history`'de kalır. fal.ai Flux'a düşme varsayılan kapalı
  (`REFERENCE_FAL_FALLBACK`).
- **Kuyruk sırası (RunPod gönderiminde, proje başına):** Qwen-Image -> Qwen-Image-Edit -> FL2VA -> Ref2VA. Projede başka
  modelin işi bekliyor ya da çalışıyorsa yeni iş onlar bitene dek bekler; daha öncelikli modelin işi bekliyorsa çalışan
  grup yeni iş almaz (boşalır, sıra ona geçer); proje boştayken aynı anda gelenler 1 sn toplanır. Videodan sonra gelen bir
  kare (yeniden deneme) çalışan video grubu bitince gider ("model sırasını bekliyor" olayı).

## Bilinen sınırlar

- Başarısız işlerin RunPod'daki süresi projeye yazılmaz (rezervasyon iade edilir); RunPod yine de faturalar.
- Sonuçlar tamamlandıktan 30 dk sonra RunPod'dan silinir. Worker çökerse yarım indirmeler bu süre içinde dakikalık görevle
  yeniden alınır; daha geç kalanlar Ayarlar › Çıktı güvencesi'nde "eksik" görünür.
- `runpod/worker-comfyui` taban imajının içi (`/comfyui` git deposu, `python -m pip`) RunPod'un deposundan okundu; derleme ilk
  kez yapılırken doğrulanmalı.
