# MiniMax H3 · ComfyUI iş akışları

Arka uç ComfyUI iken (Ayarlar › MiniMax H3 › Arka uç) Hell Grind bu klasördeki iş akışlarını ComfyUI'ye gönderir.
Dosyaları panelden yükleyin (Ayarlar › MiniMax H3 › ComfyUI iş akışları); panel doğrular ve buraya yazar.

| Dosya | Ne zaman | Gerekli yer tutucular |
|---|---|---|
| `t2v.json` | İlk karesiz video (hareket testi ve dönüş videosunun yedeği) | `{{prompt}}` |
| `i2v.json` | İlk kareden video (FL2VA: çekimler, hareket testi, düzenlenmiş kare) | `{{prompt}}`, `{{first_frame}}` |
| `r2v.json` | Karakter referanslarıyla video (Ref2VA) | `{{prompt}}`, `{{reference_1}}` |
| `keyframe.json` | Storyboard ilk karesi (bir görsel modeliyle; isteğe bağlı) | `{{prompt}}` |

`keyframe.json` için hazır örnekler `examples/` altında: `keyframe.qwen-image-2512.json` (önerilen) ve
`keyframe.flux-schnell.json`. İkisi de Apache 2.0 lisanslı modelleri kullanır ve `backend/vast/setup.sh`'nin kurduğu dosya
adlarıyla eşleşir. ComfyUI'de bir kez açıp çalıştığını görün, sonra panelden `keyframe` olarak yükleyin.

Hazırlamak için: ComfyUI'de H3 şablonunu (Template Library › MiniMax H3) açın, çalıştığını görün, sonra
**Workflow › Export (API)** ile kaydedin. JSON'u bir metin düzenleyicide açıp değişen değerleri yer tutucuyla değiştirin:

- `{{prompt}}` istem metni (metin kutusu), `{{seed}}` tohum (sayı), `{{width}}` / `{{height}}` tuval (480p: 864x480,
  720p: 1280x704), `{{frames}}` kare sayısı (17k+5: 4 sn 107, 5 sn 124), `{{duration}}` saniye, `{{fps}}` (24),
  `{{steps}}` adım sayısı, `{{filename_prefix}}` kaydetme düğümünün dosya öneki.
- `{{first_frame}}` ve `{{reference_1}}` ... `{{reference_9}}`: LoadImage düğümlerinin `image` girdisi. Hell Grind görseli
  önce `/upload/image` ile yükler, adını buraya koyar. İş akışındaki referans yuvası gönderilen görselden fazlaysa fazlası
  son görselle doldurulur.

Sayısal bir girdinin tamamı tek yer tutucuysa (`"seed": "{{seed}}"`) değer sayı olarak konur. Çıktı için iş akışında bir
video (ya da `keyframe.json`'da görsel) kaydetme düğümü olmalı. Bu dosyalarda gizli bilgi yoktur.
