# MiniMax H3 · ComfyUI iş akışları

Arka uç ComfyUI iken (Ayarlar › MiniMax H3 › Arka uç) Hell Grind bu klasördeki iş akışlarını ComfyUI'ye gönderir.
Dosyaları panelden yükleyin (Ayarlar › MiniMax H3 › ComfyUI iş akışları); panel doğrular ve buraya yazar.

| Dosya | Ne zaman | Gerekli yer tutucular |
|---|---|---|
| `t2v.json` | İlk karesiz video (hareket testi ve dönüş videosunun yedeği) | `{{prompt}}` |
| `i2v.json` | İlk kareden video (FL2VA: çekimler, hareket testi, düzenlenmiş kare) | `{{prompt}}`, `{{first_frame}}` |
| `r2v.json` | Karakter referanslarıyla video (Ref2VA) | `{{prompt}}`, `{{reference_1}}` |
| `keyframe.json` | Storyboard ilk karesi (bir görsel modeliyle; isteğe bağlı) | `{{prompt}}` |
| `edit_keyframe.json` | Karakterli çekimin ilk karesi, karakterin referansından (Qwen-Image-Edit-2511) | `{{prompt}}`, `{{reference_1}}` |

**Hazır iş akışları (ComfyUI 0.39.0'ın resmî şablonlarından):** bu klasördeki `t2v.json` (yalnızca metin,
`video_minimax_h3_t2v`), `i2v.json` (FL2VA, `video_minimax_h3_i2v`), `r2v.json` (Ref2VA, `video_minimax_h3_r2v`, 9 referans
yuvası), `keyframe.json` (Qwen-Image-2512, `image_qwen_Image_2512`) ve `edit_keyframe.json` (Qwen-Image-Edit-2511,
`image_qwen_image_edit_2511`, workflow-templates v0.11.78). `exported/` altında ComfyUI'nin dışa aktardığı hâlleri durur; `exported/make_templates.py` onlardan
bu dosyaları üretir:

- video VAE `minimax_h3_video_vae_fp16` (volume'daki dosya; int8_convrot VAE cu130 ister),
- H3 DiT `minimax_h3_{fl2va,ref2va}_pruned_{{unet_variant}}`: Ayarlar › H3 DiT türü (`fp8_scaled` varsayılan, cu128
  worker; `int8_convrot` cu130 torch'lu worker ister),
- turbo LoRA açık (FL2VA ve T2V 8 adım, Ref2VA 4 adım),
- Qwen anahtar karesinde volume'da olmayan Lightning LoRA çıkarıldı (50 adım, cfg 4; Qwen negatif istemi kullanır, şablonun
  negatifi korunur),
- tuval `{{width}}` / `{{height}}` (ResolutionSelector yerine), FL2VA'daki bağlantısız düğümler silindi,
- Qwen-Image-Edit karesinde gizli latent `EmptySD3LatentImage` (H3 tuvali; şablonda 1. görselin VAEEncode'u çıktıyı onun
  boyutuna bağlardı); `41` = image 1 (ana karakter), `83` = image 2, `171` = image 3; her görsel iki metin kodlayıcısına
  (`170:151` olumlu, `170:149` olumsuz `{{negative}}`) aynı sırayla gider; gönderilmeyen görselin düğümü ve `image2` /
  `image3` girdileri istekten çıkarılır. Lightning anahtarı `{{lightning}}` (Ayarlar › `QWEN_EDIT_LIGHTNING`).

RunPod worker'ı ComfyUI 0.39.0'a sabitlidir (`backend/runpod/worker/Dockerfile`): düğüm ve girdi adları bu sürüme göredir.
ComfyUI'yi yükseltirseniz şablonları yeniden dışa aktarıp `make_templates.py`'yi çalıştırın.
`examples/keyframe.flux-schnell.json` FLUX.1 [schnell] seçeneğidir.

İleride (ses referansı): 0.39.0'daki `MiniMaxH3ReferenceToVideo` görsellerin yanında `ref_videos` (en çok 3; 24 fps, 2-15 sn),
`ref_video_audios` ve `ref_audios` (en çok 3) da alır; Hell Grind bunları henüz göndermiyor.

Hazırlamak için: ComfyUI'de H3 şablonunu (Template Library › MiniMax H3) açın, çalıştığını görün, sonra
**Workflow › Export (API)** ile kaydedin. JSON'u bir metin düzenleyicide açıp değişen değerleri yer tutucuyla değiştirin:

- `{{prompt}}` istem metni (metin kutusu), `{{seed}}` tohum (sayı), `{{width}}` / `{{height}}` tuval (480p: 864x480,
  720p: 1280x704), `{{frames}}` kare sayısı (17k+5: 4 sn 107, 5 sn 124), `{{duration}}` saniye, `{{fps}}` (24),
  `{{steps}}` adım sayısı, `{{filename_prefix}}` kaydetme düğümünün dosya öneki. Hazır H3 iş akışları kare sayısını
  `{{duration}}`'dan kendileri hesaplar (17k+5).
- `{{first_frame}}` ve `{{reference_1}}` ... `{{reference_9}}`: LoadImage düğümlerinin `image` girdisi. Hell Grind görseli
  önce `/upload/image` ile yükler, adını buraya koyar. FL2VA isteminde ilk kareye `<Picture 1>`, Ref2VA'da referanslara
  `<Picture 1>`, `<Picture 2>`... diye başvurulur. Gönderilen görselden fazla referans yuvası, yalnızca isteğe bağlı bir
  girdiye (`ref_images.*`) bağlıysa istekten çıkarılır; değilse son görselle doldurulur.

Sayısal bir girdinin tamamı tek yer tutucuysa (`"seed": "{{seed}}"`) değer sayı olarak konur. Çıktı için iş akışında bir
video (ya da `keyframe.json`'da görsel) kaydetme düğümü olmalı. Bu dosyalarda gizli bilgi yoktur.
