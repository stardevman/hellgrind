# Hell Grind — Otonom AI Animasyon Fabrikası: Geliştirme Talimatı

## Rolün
Kıdemli yapay zekâ sistem mimarı ve pipeline mühendisisin. Görevin, yönetmenin "Sistem Küratörü" olduğu otonom bir animasyon üretim hattını sıfırdan kurmak. Uzun vadeli hedef 90 dakikalık bir film. Bu aşamadaki hedef ise hattın tamamını **hiç para harcamadan**, yerel ve ücretsiz bileşenlerle uçtan uca çalışır hale getirmek ve bir kontrol paneliyle görünür kılmak.

Repoda mevcut kod varsa referans olarak kullanabilirsin. Ancak tek doğruluk kaynağı bu belgedir.

## Değişmez kurallar
1. **Ücretsiz mod varsayılandır.** `DRY_RUN=1` iken hiçbir ücretli API çağrılmaz; anahtar tanımlı olsa bile. Ücretli kodun her giriş noktası `app/live/guard(key_name)` fonksiyonundan geçer. Bu fonksiyon DRY_RUN=1 ise veya anahtar boşsa `PaidCallBlocked` fırlatır. Tek istisna panelin **Bağlantıyı sına** düğmesidir (`app/live/probe.py`): yalnızca elle çalışır, yalnızca ücretsiz ve salt okunur doğrulama uçlarına gider (H3 için vast.ai makine durumu okuması ve sunucunun sağlık ucu dahil), üretim yapmaz (RunPod için bakiye, uç nokta ayarı ve sağlığı okuması dahil; worker başlatmaz). RunPod uç nokta ayarını yazma, vast.ai makinesini başlatma, durdurma ve boşta durdurma ücretli kilitten (`guard("runpod_api_key")` / `guard("vast_api_key")`) geçer: yalnızca ücretli modda çalışır.
2. **Ücretli kod yalnızca `app/live/` altında durur** ve yalnızca fonksiyon içinde, lazy import ile yüklenir. Testler, duman testi ve CI bu klasördeki hiçbir ağ çağrısını çalıştırmaz. Benden açık onay almadan `DRY_RUN=0` ile hiçbir şey çalıştırma. Ücretli moda geçiş yalnızca yönetmenin kendisi tarafından yapılır: `.env` elle düzenlenerek ya da panelin **Ayarlar** sayfasında onay ifadesi ("ücretli moda geç") yazılarak; üretim sürerken geçiş reddedilir. Testler `.env` ne derse desin her zaman ücretsiz modda koşar.
3. **Her ücretli (ya da ücretsiz modda sanal) çağrı bütçe rezervasyonundan geçer:** önce `reserve`, sonra `commit` ya da hata durumunda `release`. Bütçe ücretsiz modda da gerçek sağlayıcı fiyatlarıyla sanal olarak tutulur.
4. **Worker'lar durumsuzdur.** Girdi = veritabanı satırları + nesne deposu anahtarları. Çıktı = içerik-adresli yeni bir anahtar + veritabanı güncellemesi. Aynı girdi aynı anahtarı üretir; dosya zaten varsa iş atlanır.
5. **Model eksik bilgiyi uydurur.** Bir çekimde adı geçen her karakter, mekân ve nesnenin onaylı referansı yoksa üretim başlamaz (Faz 12).
6. **Stil tek yerden yönetilir.** Işık dili, doku kuralları ve hareket tarzı projenin sürümlü stil kılavuzundan gelir; çekim bazında elle yazılmaz (Faz 13).
7. **Yönetmenin her kürasyon kararı kaydedilir** (seçim, eleme, iyileştirme işareti, onay). Bu veri ileride projeye özel kalite modelinin temelidir (Faz 17).
8. Gizli anahtarlar yalnızca `.env` içinde durur ve `.env` asla commit edilmez. Panelin Ayarlar sayfası yalnızca beyaz listedeki ücretli servis ayarlarını (anahtarlar, model ve uç noktalar, motor seçimi, mod) bu dosyaya yazar; anahtarlar hiçbir yanıtta, günlükte ya da kayıtta açık görünmez ve değerleri yalnızca `app/live/__init__.py` içinden okunur.
9. Kullanıcıya görünen tüm metinler Türkçe; kod, tanımlayıcılar ve commit mesajları İngilizce olabilir.
10. Her fazın sonunda testler geçmeli, sonra commit atılmalı. Bir faz bitmeden sonrakine geçme.

## Teknoloji yığını
- **Python 3.12:** FastAPI, SQLAlchemy 2.0 (senkron, psycopg 3), Celery 5 + Redis, boto3 (MinIO/S3), Pillow, numpy, PyYAML, httpx, pytest.
- **Altyapı:** PostgreSQL 16, Redis 7, MinIO (yerel S3).
- **Medya:** FFmpeg ve ffprobe, espeak-ng (ücretsiz Türkçe konuşma), fonts-dejavu-core.
- **Panel:** Next.js (App Router) + TypeScript + SWR, `frontend/` altında. Statik dışa aktarım (`output: export`, `basePath: /panel`) Docker imajının ilk aşamasında Node 22 ile derlenir ve FastAPI tarafından `/panel` altında servis edilir; çalışma anında Node yoktur.
- **Ücretli, kilitli entegrasyonlar** (ayrı `requirements-live.txt`): fal-client, langgraph, langchain-anthropic.
- **Video motoru (ücretli modda, Ekim 2026'dan):** fal.ai kuyruğunda taslak **MiniMax H3 Max** (`h3max`, yeni çekimlerin varsayılanı), final **Seedance 2.5** (`seedance25`, "Seedance 2.5 ile yükselt"); kareler fal.ai'de Qwen-Image-2512 / Qwen-Image-Edit-2511 (`KEYFRAME_PLATFORM=fal`). Girdi varsayılan olarak **referans modu**dur (`VIDEO_INPUT_MODE=reference`: hiçbir kare ya da görsel üretilmez, video onaylı referans görselleri + senaryodan; `SCENE_RENDER_MODE=whole_scene`: sahnenin çekimleri tek istekte, render'dan önce plan onayı). Kendi barındırdığımız H3 **kapalıdır** (`H3_PLATFORM=kapali`; RunPod uç noktası ve volume silindi, kod duruyor). Eski açıklama:
  MiniMax H3, **RunPod Serverless**'ta (o zamanki varsayılan platform; `/run` + `/status`, httpx ile): modeller bir Network Volume'da (CA-MTL-1, `backend/runpod/setup_volume.sh`), worker RunPod'un resmî ComfyUI worker'ı tabanlı imaj (`backend/runpod/worker`). vast.ai arka ucu (ComfyUI ya da SGLang, `backend/vast/setup.sh`) deneyseldir ve varsayılan olarak kapalıdır (`H3_PLATFORM=vast`). Bölgeler izinli listeyle denetlenir (`app/core/regions.py`). fal.ai isteğe bağlıdır: Seedance (yönetmenin seçtiği çekimler), Flux kareleri, previs aktarımı, dudak senkronu.

## Repo yapısı
- `docker-compose.yml`: postgres, redis, minio, api, worker, worker-gen, beat, flower servisleri. Ortak backend ayarları YAML anchor ile paylaşılır; panel kaynağı `additional_contexts: frontend` ile imaja verilir.
- `frontend/`: Next.js paneli (`src/app`, `src/components`, `src/lib`, vitest testleri `tests/`).
- `.env.example`, `.gitignore`, `Makefile`, `README.md` (Türkçe), `.github/workflows/ci.yml`.
- `db/init/001_core.sql`: ilk şema; PostgreSQL ilk açılışta uygular.
- `backend/Dockerfile`, `requirements.txt`, `requirements-live.txt`, `pricing.yaml`, `migrations/*.sql`.
- `backend/app/` altında modüller:
  - `config.py`, `db.py`, `models.py`, `schemas.py`, `storage.py`, `migrate.py`, `main.py`
  - `core/` (hashing, rerender), `budget/` (killswitch, pricing)
  - `hermes/` (rules, ingest, lint), `style/` (bible, composer)
  - `engine/` (procedural, providers, iteration), `quality/` (anomaly, gate, consistency)
  - `post/` (audio, mux, grade, pipeline, timeline, export)
  - `api/` (projects, scenes, assets, sequences, budget, panel), `worker/` (celery_app, tasks, dispatch)
  - `live/` (`__init__` içinde guard, runpod_h3, vast_h3, vast_box, vast_api, h3_outputs, tls_pin, fal_video, elevenlabs, hermes_llm, probe)
  - `core/regions.py` (izinli bölgeler; ağ yok)
- `backend/scripts/smoke.py` ve `backend/tests/`.

---

# Bölüm A — Temel hat (Faz 0–10)

## Faz 0 — İskelet ve altyapı
- docker-compose: postgres (healthcheck, `db/init` bağlı), redis (appendonly, noeviction), minio (9000 ve 9001 portları).
  - api: uvicorn, 8000 portu.
  - worker: `celery -A app.worker.celery_app worker -Q post,control -c 4`.
  - worker-gen: `celery -A app.worker.celery_app worker -Q gen --pool threads -c 4` (üretim tek süreçte; kalite
    skorlayıcısı ~3,6 GB, süreç başına bir kez yüklenir).
  - beat ve flower (5555 portu).
- Backend imajı python:3.12-slim tabanlı olsun ve ffmpeg, espeak-ng, fonts-dejavu-core kursun.
- `config.py` pydantic-settings ile yazılsın. Ayarlar ve varsayılanları:
  - Bağlantılar: `database_url`, `redis_url`, `s3_endpoint_url=http://minio:9000`, `s3_public_endpoint_url=http://localhost:9000`, `s3_bucket`.
  - Ücretsiz mod: `dry_run=True`, `dry_run_defect_rate=0.12`, `dry_run_fail_rate=0`, `dry_run_score_mean=0.89`, `dry_run_score_sd=0.05`, `local_render_fps=24`.
  - Davranış: `gen_concurrency=3`, `purge_mode=quarantine`, `tts_engine=espeak`, `hermes_mode=rules`.
  - Kilitli anahtarlar: `fal_key`, `anthropic_api_key`, `elevenlabs_api_key`, `hermes_model`.
- `DEFAULT_PROJECT_SETTINGS`:
  - Eşikler: `clip_threshold=0.85`, `excellent_threshold=0.92`, `min_pass_rate=0.3`.
  - Varyasyon sayıları: `probe_n=10`, `full_n=50`, `wave_size=10`, `refined_k=3`, `max_correction_rounds=2`.
  - Çözünürlükler: `probe_resolution=480p`, `full_resolution=720p`.
  - Bütçe ve ses: `soft_budget_ratio=0.8`, `max_spend_per_10min=20`, `loudness_lufs=-23`.
- CI: ubuntu-latest + ffmpeg + espeak-ng, `pip install -r requirements.txt`, `DRY_RUN=1 pytest -q`.

**Kabul:** `docker compose up -d --build` sonrası tüm servisler ayakta ve `/healthz` şu yanıtı döner: `{"ok": true, "dry_run": true}`.

## Faz 1 — Veri modeli ve migration
ENUM tipleri:
- `project_status`: draft, parsing, producing, paused, killed, completed
- `scene_status`: pending, parsed, probing, generating, correcting, refined, needs_review, audio, lipsync, muxing, done, failed, frozen
- `engine_kind`: fal_wan, seedance, higgsfield, h3 (h3 migration 011 ile eklendi; yeni çekimlerin varsayılanı)
- `variation_tier`: probe, full
- `variation_status`: queued, running, generated, passed, rejected, purged, refined, failed
- `audio_kind`: dialogue, music, sfx, ambience
- `emotion_tag`: neutral, joy, anger, fear, sadness, surprise, disgust, tension
- `ledger_kind`: reserve, commit, release

Tablolar (UUID birincil anahtar `gen_random_uuid()`, JSONB alanlar):
- **projects:** title, status, budget_limit_usd, spent_usd, reserved_usd, settings, timeline_key, script_source, zaman damgaları.
- **characters:** project_id, name (proje içinde benzersiz), description, lora_url, lora_trigger, lora_scale, reference_images, voice_id, pinecone_namespace, embedding_version, style_constraints.
- **scenes:**
  - Kimlik: project_id, scene_code (`S001_SH01` biçiminde, proje içinde benzersiz), seq_order, script_text, description.
  - Çekim parametreleri: emotion, camera (`{shot, movement, angle, lens_mm}`), action_complexity (0–1), engine, duration_s, prompt_spec, prompt_revision, correction_count, status.
  - Stateless Re-render alanları: visual_hash, dialogue_hash, bed_hash, mux_hash, lipsync_video_key, final_video_key.
  - Diğer: spent_usd, last_error.
- **scene_characters:** scene_id, character_id, role, screen_position, dialogue.
- **variations:**
  - Üretim bilgisi: scene_id, tier, wave_no, engine, seed, prompt_revision, params, external_request_id, keyframe_key (küçük resim), video_key, status.
  - Skorlar ve sonuç: clip_score, face_score, composite_score, anomalies, refined_rank (1–3), cost_usd.
  - İndeksler: `(scene_id, refined_rank)` üzerinde kısmi benzersiz indeks.
- **audio_tracks:** scene_id, character_id, kind, source, text, emotion, params, content_hash, file_key, duration_s, start_offset_s, gain_db, is_active, version.
- **cost_ledger:** project_id, scene_id, variation_id, provider, kind, amount_usd, reservation (UUID), created_at.

`migrate.py`, `backend/migrations/*.sql` dosyalarını advisory lock altında, `schema_migrations` tablosuyla sırayla ve bir kez uygular. API açılışında çalışır. Sonraki fazlardaki tüm şema değişiklikleri yeni migration dosyalarıyla eklenir.

**Kabul:** Mevcut bir veritabanı silinmeden yeni sütunlar eklenebiliyor.

## Faz 2 — Kill-Switch (bütçe koruması)
- **`reserve`:** tek atomik UPDATE ile çalışır: `reserved += amt WHERE status NOT IN (paused, killed) AND spent + reserved + amt <= limit RETURNING`.
  - Satır dönmezse projeyi dondurur ve `BudgetExceeded` fırlatır.
  - Başarılı olursa defterine `reserve` kaydı düşer.
- **`commit`:** reserved'dan tahmini tutarı düşer, spent'e gerçek tutarı ekler, deftere ve sahne harcamasına yazar.
  - `spent >= limit × soft_budget_ratio` olunca Redis'e yumuşak limit bayrağı koyar.
  - `spent >= limit` olunca projeyi dondurur.
- **`release`:** başarısız çağrıda rezervasyonu iade eder.
- **`spend()` context manager:** reserve, commit ve release adımlarını tek kullanımda birleştirir.
- **`freeze(reason)`:** Redis bayrağını kurar, projeyi `paused` yapar, aktif sahneleri `frozen` yapar. Redis'te proje başına tutulan görev kimliklerini Celery ile revoke eder ve bir olay yazar.
- **`resume`:** freeze'in tersini yapar.
- **`is_frozen`:** önce Redis'e, sonra veritabanına bakar.
- **Celery beat görevleri:**
  - Hız bekçisi (30 sn'de bir): 10 dakikalık commit toplamı limiti aşarsa projeyi dondurur.
  - Bayat rezervasyon toplayıcı (5 dk'da bir): 60 dakikadan eski, eşi olmayan rezervasyonları iade eder.
- **Olaylar:** Redis'te `hg:events:{project}` listesi, son 200 kayıt.
- **`pricing.yaml`** (USD; fal.ai fiyatları, Ekim 2026, periyodik güncellenmeli):
  - `self_hosted.h3`: kiralanan GPU makinesinin saatlik ücreti (`gpu_hourly_usd`, 0.95) ve video saniyesi başına GPU saniyesi (`render_seconds`: 480p 8, 720p 16.5; yönetmenin ölçümü: ComfyUI, RTX 5090, int8_convrot, Turbo 8 adım), storyboard karesi için `keyframe_seconds`. Rezervasyon bunlardan tahmin edilir; işlenen tutar sunucunun bildirdiği GPU süresi × saatlik ücrettir. Makinenin boşta geçen süresi (kuyruk boşken) aynı ücretle son etkin projeye yazılır.
  - fal_wan: 480p için 0.04, 720p için 0.08 (video saniyesi başına).
  - seedance: 720p için 0.242/sn.
  - higgsfield: 720p için 0.25/sn (tahmini).
  - flux_lora: 0.035/MP.
  - lipsync: 5.0/dk.
  - Bilinmeyen çözünürlükte o motorun en pahalı fiyatı kullanılır.

## Faz 3 — Stateless Re-render
- `stable_hash`: sıralı anahtarlı JSON'un sha256'sı, ilk 32 karakter. Python'un `hash()` fonksiyonu kullanılmaz.
- `seed_for(*parts)`: 31-bit, tekrarlanabilir seed.
- Özetler:
  - `visual_fingerprint`: prompt_spec, camera, engine, duration ve karakterlerin (id, embedding_version, lora_url, lora_scale) bilgileri. prompt_revision dahil edilmez.
  - `audio_fingerprint`: aktif kanalların (kind, content_hash, offset, gain) bilgileri.
  - Diyalog ve fon (müzik/sfx/ambiyans) ayrı özetlenir.
- `plan_rerender` kuralları:
  - Görsel özet değiştiyse ya da yoksa: VISUAL + LIPSYNC + MUX.
  - Değilse, lipsync videosu yoksa ya da diyalog değiştiyse: LIPSYNC + MUX.
  - Değilse, final video yoksa ya da fon değiştiyse: yalnızca MUX.
  - Hiçbiri değilse: hiçbir aşama çalışmaz.
- `build_chain`: aşamalara karşılık gelen Celery görevlerini `.si()` ile zincirler.
- Görevler: `generate_scene` (gen kuyruğu), `lipsync_scene`, `mux_scene` ve `assemble_timeline` (post kuyruğu).
- Ortak koruma sarmalayıcısı: frozen kontrolü, görev kaydı ve hata durumunda `last_error` yazımı.
- Sonraki fazlarda STORYBOARD (Faz 14) ve GRADE (Faz 16) aşamaları bu planlayıcıya eklenecek; tasarımı genişletilebilir tut.

**Kabul:** Birim testleri şunları doğrular: yalnızca müzik değişince sadece MUX; diyalog değişince VISUAL çalışmaz; karakter sürümü artınca tüm aşamalar; değişiklik yoksa hiçbir aşama.

## Faz 4 — Hermes: senaryo ayrıştırma (ücretsiz, kural tabanlı)
- **Sahne başlığı:** `İÇ.`, `DIŞ.`, `İÇ/DIŞ.`, `INT.` veya `EXT.` ile başlayan satır.
- **Konuşan karakter:** büyük harfli, en fazla 4 kelime ve 40 karakterlik satır; `(D.S.)` gibi uzantılar atılır.
- **Parantez satırı:** duygu ipucu.
- **Diğer paragraflar:** aksiyon, her biri yeni bir çekim.
- **Türkçe büyük/küçük harf:** `tr_lower` ve `tr_title` I/ı ve İ/i dönüşümlerini doğru yapar ("AYŞE" → "Ayşe", "IŞIL" → "Işıl").
- **Diyaloğun çekime bağlanması:** konuşan karakter son çekimde varsa, çekimde 2'den az replik varsa ve toplam süre 8,5 saniyeyi aşmıyorsa son çekime eklenir. Aksi halde yeni bir yakın plan çekim açılır.
- **Duygu:** Türkçe kök sözlüğünden çıkarılır (ör. kork→fear, öfke/bağır→anger, tedirgin/gergin→tension).
- **Kamera ve aksiyon kuralları:**
  - Aksiyon kelimeleri (koş, atla, patla, düş, kop, sarsıl…) varsa: wide + handheld/tracking, complexity 0.8.
  - Sahnenin ilk çekimi: geniş açı ve pan.
  - Yalnızca diyalog: close_up; duygu yoğunsa dolly_in.
  - Diğer: medium.
- **Süre:** diyalog için kelime / 2,5 + boşluklar; 3–10 saniye arasında sınırlanır.
- **Çıktı:** `{cast, shots:[{scene_id, heading, description, visual_prompt, camera, emotion, characters, dialogue, duration_s, action_complexity}]}`.
- **`ingest`:**
  - Karakterleri ve sahneleri upsert eder, sahne-karakter bağlarını yazar.
  - Diyalog kanallarını ardışık offset'lerle oluşturur. İçerik değişmediyse kanalları korur, böylece gereksiz yeniden render olmaz.
  - Sahnede müzik kanalı yoksa duygusuna göre prosedürel bir müzik kanalı ekler (−8 dB). Faz 11'de müzik sekans düzeyine taşınacak.
- **`SAMPLE_SCRIPT`:** 2 sahne, 4 çekim, Kerem ve Ayşe karakterleri; döküm hanesi ve çöken köprü mekânları.

**Kabul:** Örnek senaryo 4 çekim ve `["Kerem","Ayşe"]` kadrosu verir; köprü çekiminin action_complexity değeri 0.7 veya üstüdür.

## Faz 5 — Yerel prosedürel render ve sağlayıcı soyutlaması
`engine/procedural.render_clip(out, seed, width, height, duration, fps, emotion, camera, characters, action, label)`:
- **Dünya:** çıktının 1,6 katı çözünürlükte, 2 katman (Pillow ile).
  - Arka katman: gökyüzü gradyanı, uzak fabrika siluetleri ve bacalar, ufuk parıltısı.
  - Ön katman: kirişler, köprü, zemin, perspektif çizgileri.
- **Duygu paletleri:** 8 duygu için (gökyüzü, ufuk, zemin, vurgu) renk dörtlüsü; öfke, gerilim ve korkuda kor parçacıkları.
- **Kamera:**
  - Çekim ölçeğinden zoom: extreme_wide 1.0 … extreme_close_up 3.0.
  - Hareketler: dolly_in, dolly_out, pan, tilt, crane, orbit, tracking, handheld (yumuşatılmış titreme).
  - Parallax: arka katman 0.35 hızında kayar.
- **Karakterler:** adlı siluetler; aksiyon 0.6 üstündeyse koşarlar.
- **Görüntü:** vinyet ve sol alt köşede çekim etiketi. Kareler raw RGB olarak FFmpeg'e pipe edilir (libx264, yuv420p, faststart).
- **`render_defect`:** Quality Gate'i sınamak için bilerek siyah ya da donmuş klip üretir.
- **Sağlayıcılar:**
  - `LocalProvider`: ücretsiz. `dry_run_defect_rate` oranında bozuk klip üretir. Maliyet, motorun gerçek fiyatından sanal olarak hesaplanır.
  - `get_provider`: DRY_RUN açıkken her zaman LocalProvider döner; kapalıyken `app.live.fal_video` modülünü lazy import eder.
  - `route_engine`: yeni çekim taslak motoruna gider (`VIDEO_DRAFT_ENGINE`, varsayılan h3max; proje ayarı `draft_engine` önce gelir). Seçilmiş motor (h3max, seedance25, seedance, h3) yeniden yönlendirmede korunur; eski fal_wan çekimleri taslak motoruna geçer.

## Faz 6 — Kademeli iterasyon ve Quality Gate
- **Test turu:** `probe_n` varyasyon, `probe_resolution` çözünürlüğünde.
  - Kabul koşulu: ortalama birleşik skor ≥ clip_threshold ve geçme oranı ≥ min_pass_rate.
  - Kabul edilmezse yeni seed'lerle tekrar denenir (en fazla `max_correction_rounds`). Yine olmazsa sahne `needs_review` olur.
- **Tam tur:** `wave_size`'lık dalgalarla `full_n` üst sınırına kadar üretilir.
  - Erken durma: `excellent_threshold` üstünde `refined_k` aday bulununca.
  - Yumuşak limit bayrağı varsa yeni dalga başlatılmaz.
- **Her varyasyon için akış:**
  1. Frozen kontrolü.
  2. `seed_for(scene_id, visual_hash, tier, wave, idx)`.
  3. Variation satırı oluşturma.
  4. `spend()` içinde üretim (geçici hatalarda 3 deneme).
  5. Nesne deposuna yükleme ve küçük resim (`thumbs/...jpg`, 320 px, 2. saniyeden).
  6. Skorlama.
  7. Eşiği geçemeyenin anında imhası: `quarantine` modunda `quarantine/` önekine taşınır ve `params.purge_reason="quality_gate"` yazılır.
- **Eşzamanlılık:** her dalga `ThreadPoolExecutor(gen_concurrency)` ile çalışır.
- **Anomali tespiti** (gerçek, FFmpeg): `blackdetect` (d=0.5, pix_th=0.10) ve `freezedetect` (n=0.003, d=2), sonuç stderr'den okunur.
- **Skor:** ücretsiz modda seed'e bağlı Gauss dağılımlı sahte CLIP skoru. Eşik altı skor `low_clip` anomalisi sayılır.
- **Rafine Havuz:**
  - Yalnızca tam turda geçenlerden en iyi `refined_k` varyasyon rank 1..n alır.
  - Kalanlar `purge_reason="not_top_k"` ile imha edilir.
  - Sahne `refined` durumuna geçer ve visual_hash yazılır.

## Faz 7 — Ses ve montaj (ücretsiz)
- **Konuşma:** `espeak-ng -v tr+<varyant> -p <perde> -s <hız>`.
  - Duygu perde ve hızı belirler.
  - Karakter kimliğinin özetinden ses varyantı seçilir; böylece her karakterin sesi tutarlı kalır.
  - espeak-ng yoksa yapay tona düşülür.
- **Müzik:** duyguya göre akor; FFmpeg `aevalsrc` ile 0,25 Hz tremolo, fade-in ve lowpass.
- **Mux** (FFmpeg filter_complex):
  - Her kanal: 48k stereo, `adelay`, `volume`.
  - Diyaloglar ve fon ayrı ayrı amix ile birleşir.
  - Diyalog `sidechaincompress` ile müziği bastırır (ducking).
  - `loudnorm I=-23 TP=-1.5 LRA=11`, video kopyalanır, aac 192k, süre videoya eşitlenir.
  - Ses yoksa video sessiz kopyalanır.
- **LIPSYNC aşaması:** eksik diyalog seslerini üretir ve yükler.
  - Anahtar: `lipsync/{scene}/{variation_id}_{dialogue_hash}.mp4`.
  - Ücretsiz modda rank 1 video olduğu gibi kopyalanır.
- **MUX aşaması:** `final/{scene}/{mux_hash}.mp4`; sahne `done` olur.

## Faz 8 — Film birleştirme
- Tamamlanan sahneler `seq_order` sırasıyla alınır.
- Her klip 1280×720, 24 fps, yuv420p'ye normalize edilir; sesi yoksa `anullsrc` ile sessiz ses eklenir.
- Klipler concat demuxer ile `-c copy` birleştirilir.
- Çıktılar: `timeline/{project}/{hash}.mp4` ve `.json` kesme listesi (scene_code, in_s, out_s, description). `projects.timeline_key` güncellenir.

## Faz 9 — API ve kontrol paneli
**Uç noktalar** (`/api` öneki):
- Genel: `GET /meta` (dry_run, ön ayarlar, örnek senaryo).
- Projeler: `GET` ve `POST /projects`, `GET /projects/{id}`, `POST /projects/{id}/characters`, `POST /projects/{id}/scenes`.
- Senaryo: `POST /projects/{id}/script` (ingest + otomatik render), `POST /projects/{id}/render-all`.
- Panel verisi: `GET /projects/{id}/overview`. Paneli tek çağrıda besler: sahneler, varyasyonlar (küçük resim ve video URL'si, purge nedeni), kanallar, olaylar, zaman çizelgesi.
- Sahneler:
  - `GET` ve `PATCH /scenes/{id}`.
  - `POST /scenes/{id}/render?force_visual=`: frozen projede 423 döner.
  - `PUT /scenes/{id}/audio`: yeni kanal sürümü + plan + dispatch; planlanan aşamaları döner.
  - `GET /scenes/{id}/variations`.
  - `POST /scenes/{id}/promote/{variation_id}`: rafine sırasını değiştirir, lipsync_video_key'i sıfırlar; yalnızca LIPSYNC + MUX çalışır.
- Medya: `GET /media?key=`.
  - jpg ve json: doğrudan, immutable önbellekle.
  - Video: herkese açık uç noktayla imzalanmış URL'ye 307 yönlendirme. SigV4 host'a bağlı olduğu için imzalama ayrı bir public client ile yapılır.
- Bütçe: `GET /projects/{id}/budget`, `POST /projects/{id}/killswitch {freeze|resume}`.
- Film: `POST /projects/{id}/assemble`.
- Ön ayarlar:
  - preview: probe_n 4, full_n 8, wave_size 4, max_correction_rounds 2.
  - brief: proje varsayılanları.

**Panel** (`/` adresi `/panel/` adresine yönlenir; 2 saniyede bir overview çeker). İlk sürüm tek HTML dosyasıydı; Next.js'e
geçişle birlikte aşağıdaki işlevler bir yapım stüdyosu düzenine taşındı: alt kenarda üretim sırasıyla sayfa çubuğu (Senaryo,
Stil, Varlıklar, Storyboard, Kurgu, Karşılaştır, Renk, Teslim); Kurgu sayfasında çekim havuzu, zaman kodlu izleyici, adaylar,
açılır denetçi ve görüntü/diyalog/müzik izli zaman çizelgesi. Sekiz sayfa dört **adım** altında gruplanır (Hazırlık: Senaryo,
Stil, Varlıklar; Onay: Storyboard; Üretim: Kurgu, Karşılaştır; Bitirme: Renk, Teslim; `lib/steps.ts`): her adımın durumu ve
eksikleri overview'dan hesaplanır, çubuğun sağındaki **Sıradaki** bağlantısı ilk eksik işe götürür. Üst bantta **Basit /
Gelişmiş** görünüm anahtarı (basit görünümde yalnızca ana eylemler; motor ve plan ayrıntıları, tamamlama ve upscale, elle
düzeltme ve Karşılaştır sayfası gelişmişte) ve **Rehber**: ilk açılışta kendiliğinden başlayan, `data-tour` hedeflerine
bağlı tur (`lib/tour.ts`, `components/Tour.tsx`). Denetçi üç sekmedir: Çekim, Ses, Üretim. İşlevler ve metinler aşağıdaki
gibidir:
- **Üst bant:** marka, proje seçici, "Yeni proje" ve "Ücretsiz mod: hiçbir ücretli servis çağrılmaz" etiketi.
- **Bütçe rayı:** harcanan ve ayrılan tutar; "Üretimi durdur" ve "Üretime devam et" düğmeleri; dondurulmuşsa uyarı bandı.
- **Sahne satırları:** kod, durum, açıklama, **film şeridi** ve harcama.
  - Şeritte test turu karelerinden sonra tam tur kareleri gelir.
  - Rafine kareler yeşil çerçeveli ve numaralı; kalite kontrolünde elenenler kırmızı çapraz çizgili ve gri; havuza giremeyenler soluk; üretilenler nefes alan amber renkte.
  - Bir kareye tıklayınca o varyasyon (karantinadakiler dahil) önizlenir.
- **Denetçi paneli:** sonuç videosu (poster = rank 1 küçük resmi), rafine havuz ("Bunu kullan"), çekim bilgileri, diyalog listesi, "Müziği değiştir" (duygu + dB) ve "Bu sahneyi baştan üret".
  - Her eylemden sonra hangi aşamaların çalışacağı yazılır, ör. "Yalnızca montaj; görüntü motoru çağrılmadı".
- **Film bölümü:** "Tamamlanan N sahneyi birleştir", oynatıcı ve duygu renkli, tıklanabilir kesme şeridi. Yanında olay akışı.
- **Boş durum:** proje adı, sanal bütçe, üretim yoğunluğu ve örnek senaryo dolu textarea; "Senaryoyu işle ve üret".
- **Tasarım** (kurgu ve animasyon yazılımlarının dili: grafit yüzeyler, ince çizgiler, yoğun paneller):
  - Yazı tipleri: arayüzde Inter, zaman kodu ve çekim kodlarında JetBrains Mono.
  - Renkler: bg `#111214`, panel `#1a1b1e`, raised `#212226`, line `#2c2e33`, text `#dcdee3`, accent (seçim) `#4d8dff`, playhead `#ff4a4a`, ok `#3dcc91`, warn `#f3b443`, bad `#ff5c55`, work (üretiliyor) `#a88bff`.
  - Masaüstünde tam ekran uygulama çerçevesi; dar ekranda tek sütun ve kayan sayfa. Görünür klavye odağı, `prefers-reduced-motion` desteği, kısayollar (Shift+1…8 sayfalar; J/K/L, oklar, Boşluk oynatma). Başlık üstü etiket ve büyük harf etiket kullanılmaz (Türkçe I/İ dönüşümü).

## Faz 10 — Testler
- **Birim testleri** (veritabanı gerektirmez):
  - Re-render planlayıcısı, Türkçe ayrıştırıcı, prosedürel render (geçerli süre, anomali yok, seed farkı), bozuk klip tespiti.
  - Ses (ton modu), müzik, mux (1 video + 1 ses akışı, doğru süre), sessiz mux.
  - Ücretli kilit: guard tüm anahtarlar için `PaidCallBlocked` fırlatır; `get_provider` LocalProvider döner.
- **`scripts/smoke.py`** (api konteynerinde çalışır):
  1. DRY_RUN açık ve ücretli çağrılar engelli.
  2. Örnek senaryo preview ön ayarıyla 4 sahneye ayrılır ve tüm sahneler `done` olur. Her sahnede rafine havuz 1–3 arasındadır ve siyah/donmuş klipler elenmiştir.
  3. Yalnızca müzik değişince planda sadece MUX vardır ve harcama değişmez.
  4. Küratör seçimi yalnızca LIPSYNC ve MUX çalıştırır.
  5. Değişiklik yoksa plan boştur.
  6. Film 4 sahneyle birleşir.
  7. Küçük bütçeli (0,05 USD; H3 bir klibi birkaç sentlik sanal GPU süresiyle üretir) projede sahne `frozen`, proje `paused` olur; harcama bütçeyi aşmaz ve yeni render isteği 423 döner.

**Bölüm A tamamlanma tanımı:** CI yeşil, duman testi "TÜM KONTROLLER GEÇTİ" yazıyor, panelde örnek film izlenebiliyor ve README Türkçe kurulum ile kullanım adımlarını içeriyor.

---

# Bölüm B — Profesyonel üretim akışı (Faz 11–17, tamamı ücretsiz)

Bu bölüm, Blender ve üretken yapay zekâyı birlikte kullanan profesyonel bir animasyon ekibinin (Higgsfield "PASSPORT_RUSH" kısa filmi) üretim derslerinden türetilmiştir. O ekip kısa bir film için yaklaşık 15.000 varlık üretmiştir; bizim hedefimiz aynı kaliteye çok daha az üretimle ulaşmaktır. Önerilen sıra: 12 → 13 → 11 → 14 → 15 → 16 → 17. Her faz yeni migration dosyalarıyla şemayı genişletir ve duman testine kendi kontrollerini ekler.

## Faz 11 — Sekans hiyerarşisi ve film düzeyinde ses
**Neden:** Film birkaç sekanstan oluşur (ör. Havalimanı, Taksi, Polis, Ninja, Ev). Şu an müzik her çekimde yeniden başlıyor; bu, filmin sürekliliğini bozar.
- **`sequences` tablosu:** project_id, code (`SEQ01`), title, seq_order, budget_cap_usd (boş olabilir), settings.
  - `scenes.sequence_id` eklenir.
  - Hermes, sahne başlıklarını sekanslara bağlar; senaryoda `# SEKANS: Ad` satırı varsa yeni sekans başlar, yoksa tüm sahneler tek sekansta toplanır.
- **Sekans bütçe tavanı:** `reserve`, proje limitine ek olarak sekans tavanını da kontrol eder. Tavan dolunca yalnızca o sekansın işleri durur ve olay yazılır.
- **`timeline_tracks` tablosu:** project_id, sequence_id (boş olabilir), kind (music/sfx/ambience), source (procedural/upload), file_key, params, start_s, end_s, gain_db, fade_in_s, fade_out_s, content_hash, is_active.
  - Müzik artık sekans ya da film düzeyinde yaşar ve çekimler arasında kesintisiz akar.
  - Sahne düzeyindeki fon kanalları isteğe bağlı kalır (ör. tek çekimlik efekt).
- **Film birleştirme güncellemesi:**
  1. Önce yalnızca diyalog içeren sahne klipleri birleştirilir.
  2. Ardından `timeline_tracks` kanalları film süresi üzerine yerleştirilir.
  3. Diyalog, müziği sidechain ile bastırır.
  4. Son olarak loudnorm uygulanır.
  - Zaman çizelgesi özetine kanalların content_hash değerleri dahil edilir.
- **Besteci desteği:** sekans başına zamanlanmış müzik stem'leri yüklenebilir (`POST /sequences/{id}/tracks`).
- **Panel:** sahne satırları sekans başlıkları altında gruplanır; her sekans başlığında harcama, tavan ve çekim sayısı görünür.

**Kabul:**
- Duman testinde iki sekanslı bir senaryo doğru gruplanır.
- Sekans müziği değişince hiçbir sahne yeniden render edilmez, yalnızca film yeniden birleştirilir.
- Sekans tavanı dolunca diğer sekanslar üretmeye devam eder.

## Faz 12 — Varlık kütüphanesi ve referans eksiksizliği
**Neden:** Model eksik bilgiyi uydurur. Referans verilmeyen bir taksiye model rastgele yolcular eklemiştir. Ayrıca durağan görselde iyi görünen varlıklar hareket halinde bozulabilir.
- **`assets` tablosu:** project_id, type (character/location/prop), name, description, render_style (`clean_3d` / `painterly` / `stylized_2d` / serbest metin), status, version, references, motion_test_variation_id.
  - Status akışı: `draft → still_approved → motion_tested → locked`.
  - Mevcut `characters` tablosu `assets` ile ilişkilendirilir (character_id → asset_id) ve ses alanları korunur.
- **Karakter referans paketi:** dönüş videosu (360°), ifade sayfası, ses örneği ve voice_id.
  - Yüz referansı ve vektörler dönüş videosundan eşit aralıkla örneklenen karelerden hesaplanır.
  - Ücretsiz modda paket yüklenebilir ve örnekleme yerel çalışır.
- **`scene_assets` bağ tablosu:** scene_id, asset_id, role.
  - Hermes mekânı sahne başlığından otomatik bağlar (ör. "DÖKÜMHANE" → mekân varlığı yoksa `draft` olarak oluşturulur).
  - Nesneler panelden ya da senaryodaki `[NESNE: Taksi]` işaretinden bağlanır.
  - Konuşmayan karakterler `[KARAKTER: Kerem]` işaretiyle kadroya girer; kadrodaki karakter adı geçtiği her çekime bağlanır. Senaryo İngilizce de yazılabilir (`INT./EXT.`, `[CHARACTER: ...]`, `[PROP: ...]`).
- **`hermes/lint.py` — üretim öncesi kontrol:**
  - Çekimde geçen her karakter, mekân ve nesnenin `locked` (ya da proje ayarına göre en az `still_approved`) referansı yoksa üretim başlamaz.
  - Sahne `blocked` durumuna geçer (`scene_status` enum'una eklenir) ve `last_error` alanına neyin eksik olduğu Türkçe yazılır.
  - `lint_strictness` proje ayarıyla (`off` / `warn` / `block`) kontrol edilir; varsayılan `block`.
  - Kadroda olmayan kişilerin üretilmemesi için prompt oluşturucuya "yalnızca listelenen karakterler" kısıtı eklenir.
- **Hareket testi kapısı:** bir varlık `locked` olmadan önce kısa, düşük çözünürlüklü bir hareket testi üretilir (`POST /assets/{id}/motion-test`) ve yönetmen panelden onaylar.
- **Panel:** "Varlıklar" sekmesi; tür ve durum filtresi, referans yükleme, hareket testi ve onay düğmeleri. Engellenen sahnelerde eksik varlıklar tıklanabilir bağlantı olarak gösterilir.

**Kabul:**
- Referansı eksik bir nesne içeren çekim `blocked` olur ve hiçbir harcama yapılmaz.
- Varlık kilitlenip sahne yeniden render edilince üretim başlar.
- Hareket testi onaylanmadan varlık kilitlenemez.

## Faz 13 — Stil kılavuzu, ışık dili ve hareket tarzı
**Neden:** Çekimlerin aynı filme ait görünmesini en çok tutarlı ışık dili sağlar. Profesyonel ekip "suluboya mekân + altın saat ışığı" formülünü bulup her çekimde aynı ifadeyle tekrarlamıştır. Hareket eden nesnelerde yoğun boyalı doku bozulur; bu nesneler temiz 3D, çevre ise boyalı olmalıdır.
- **`style_bibles` tablosu (sürümlü):** project_id, version, lighting_prefix, rules (ör. `{"prop": "clean_3d", "location": "painterly"}`), negative, motion_style, palette, notes, created_at, is_active.
  - Örnek ışık öneki: "Late-afternoon golden-hour lighting, low warm sun, long dramatic shadows, warm highlights, strong depth and dimensionality."
  - Örnek hareket tarzı: "cartoony motion, squash and stretch, exaggerated anticipation and follow-through" (gerçekçi fizik yerine animasyon ilkeleri).
- **`style/composer.py`:** `compose_prompt(shot, assets, bible)` deterministik çalışır.
  1. Işık öneki.
  2. Hareket tarzı.
  3. Çekim tarifi.
  4. Her varlığın kendi render_style kuralıyla tarifi.
  5. "Yalnızca listelenen karakterler" kısıtı.
  6. Negatifler.
  - Sağlayıcılara yalnızca bu fonksiyonun çıktısı gider; çekim bazında elle stil yazılmaz.
- **Görsel özete etki:** aktif stil sürümü `visual_fingerprint`'e dahil edilir.
- **Etki önizlemesi:** stil değişikliği uygulanmadan önce `POST /projects/{id}/style/preview` hangi çekimlerin yeniden render edileceğini ve tahmini sanal maliyeti döner. Yönetmen onaylarsa `POST /projects/{id}/style/activate` çalışır.
- **Yerel render desteği:** prosedürel render ışık önekindeki anahtar kelimeleri yorumlar. Örneğin "golden-hour" varsa sıcak ve alçak güneş, uzun gölgeler, ufka yakın sıcak parıltı uygular. Böylece ücretsiz modda bile stil değişikliğinin etkisi görülebilir.
- **Panel:** "Stil kılavuzu" sekmesi; sürüm geçmişi, düzenleme, etki önizlemesi ve etkinleştirme.

**Kabul:**
- Aynı stil sürümünde iki farklı çekimin promptları aynı öneki taşır.
- Stil değişince önizleme etkilenen çekimleri doğru listeler ve onay olmadan hiçbir şey render edilmez.
- Prosedürel render "golden-hour" önekiyle görünür biçimde farklı bir palet üretir.

## Faz 14 — Storyboard onayı ve taslak geçiş (kaba kurgu)
**Neden:** Profesyonel ekip önce storyboard hazırlamış, sonra filmin tam kaba kurgusunu çıkarıp yalnızca gereken sahneleri yeniden üretmiştir. Revizyonu en ucuz aşamada yapmak maliyeti en çok düşüren adımdır.
- **Yeni `STORYBOARD` aşaması** (VISUAL'dan önce):
  - Her çekim için bir storyboard karesi üretilir. Ücretsiz modda prosedürel render'dan orta kare + çekim bilgisi yazısı; ücretli modda tek bir Flux karesi.
  - Sahneye `board_status` alanı eklenir: `pending` / `approved` / `changes_requested`. `board_note` alanına yönetmen notu yazılır.
  - `require_board_approval` proje ayarı açıksa VISUAL aşaması onaysız başlamaz.
- **Storyboard PDF dışa aktarma:** sayfa başına 6 kare; sekans başlıkları, çekim kodu, kamera, süre ve diyalog yazılır (Pillow ile çok sayfalı PDF). Uç nokta: `GET /projects/{id}/storyboard.pdf`.
- **Taslak geçiş modu** (`POST /projects/{id}/draft-pass`):
  1. Tüm onaylı çekimler yalnızca test turuyla üretilir.
  2. Her çekim için geçen ilk varyasyon kullanılır ve kaba kurgu (animatik) film birleştirilir.
  3. Yönetmen panelde zayıf çekimleri "İyileştir" ile işaretler (`scenes.upgrade_requested`).
  4. `POST /projects/{id}/upgrade-pass` tam turu yalnızca işaretli çekimler için çalıştırır.
- **Panel:** "Storyboard" görünümü (kare ızgarası, onayla / değişiklik iste, not alanı). Film bölümünde "Kaba kurgu" ve "Final" sekmeleri. Her sahne satırında "İyileştir" düğmesi.

**Kabul:**
- Onay açıkken onaylanmamış çekim için hiçbir varyasyon üretilmez.
- Storyboard PDF'i tüm çekimleri doğru sırada içerir.
- Taslak geçişte tam tur hiç çalışmaz; iyileştirme geçişinde yalnızca işaretli çekimler tam tura girer.

## Faz 15 — Çekim üretim yöntemi, dış çekimler ve elle düzeltme
**Neden:** Karmaşık ya da çok özel hareketleri tarif ederek üretmek zordur. Profesyonel ekip bu çekimleri Blender'da elle animasyonla yapmış, üretilen görselleri de elle düzeltmiştir (renk, detay, artefakt temizliği, kalem çizgileri).
- **`scenes.method` alanı:**
  - `generate` (varsayılan): tamamen yapay zekâ.
  - `previs_guide`: yüklenen 3D taslak video rehber olarak kullanılır.
  - `external`: dışarıda bitirilmiş çekim yüklenir.
- **Dış çekim yükleme:** `POST /scenes/{id}/external` ile video yüklenir.
  - VISUAL aşaması atlanır, ama klip yine anomali kontrolünden, sesten, renkten ve montajdan geçer.
  - Yüklenen dosyanın içerik özeti `visual_hash` yerine geçer.
- **Rehber video:** `POST /scenes/{id}/guide` ile previs videosu yüklenir.
  - Ücretsiz modda rehber video, stil kılavuzunun renk ayarıyla doğrudan kullanılır.
  - Ücretli modda video-to-video stil aktarımı yapılır (Faz 18, kilitli).
- **Elle düzeltme döngüsü:** `POST /variations/{id}/revisions` ile düzenlenmiş bir video ya da ilk kare yüklenir.
  - Yeni revizyon sürümlenir ve orijinal korunur.
  - Rafine #1 bir revizyonsa lipsync anahtarı revizyon kimliğini içerir; Stateless Re-render yalnızca LIPSYNC ve sonrasını çalıştırır.
- **Yönlendirici:** `method` alanını okur. Önerilen kural: action_complexity çok yüksek ve hareket özelse panel previs yöntemini önerir; yönetmen seçer.
- **Panel:** sahne denetçisinde yöntem seçici, yükleme alanları ve revizyon geçmişi.

**Kabul:**
- `external` bir çekim hiçbir üretim harcaması yapmadan `done` olur.
- Bir revizyon yüklenince VISUAL çalışmaz, LIPSYNC ve MUX çalışır.

## Faz 16 — Renk aşaması ve süreklilik kontrolü
**Neden:** Profesyonel ekip her çekimi elle renklendirerek filme tutarlı bir ton vermiştir. Bunu otomatik bir temel ve uzman için dışa aktarma ile karşılıyoruz.
- **Yeni `GRADE` aşaması** (LIPSYNC ile MUX arasında):
  - Film geneli LUT (`.cube` yüklenir, FFmpeg `lut3d`) + çekim bazında ayarlar (`eq`: exposure, contrast, saturation, gamma; `colortemperature`).
  - Ayarlar `grade_hash` olarak özetlenir ve planlayıcıya eklenir: yalnızca renk değişince GRADE + MUX çalışır.
- **`quality/consistency.py`:** ardışık çekimlerin orta karelerinden HSV histogramları çıkarılır ve karşılaştırılır (ör. korelasyon ya da Bhattacharyya mesafesi).
  - Eşiği aşan geçişler panelde "renk sıçraması" olarak işaretlenir.
  - Önerilen bir düzeltme hesaplanır: bir önceki çekimin ortalama parlaklık ve sıcaklığına yaklaştıran `eq` değerleri. Yönetmen tek tıkla uygular.
- **Panel:** film bölümünde, kesme şeridinin üzerinde renk sıçraması uyarıları. Sahne denetçisinde renk ayarları ve önce/sonra karşılaştırması.

**Kabul:**
- Renk ayarı değişince VISUAL ve LIPSYNC çalışmaz.
- Bilerek farklı renklendirilmiş iki ardışık çekim uyarı üretir.
- Önerilen düzeltme uygulanınca uyarı kalkar.

## Faz 17 — Kürasyon kayıtları, verimlilik metrikleri ve profesyonel dışa aktarma
**Neden:** Rekabet avantajı, aynı kaliteye daha az üretimle ulaşmak ve yönetmen zevkini veriye dönüştürmektir. Profesyonel ekipler son dokunuşları kendi araçlarında (DaVinci, Premiere) yapmak ister.
- **`curation_events` tablosu:** project_id, scene_id, variation_id, action, payload, created_at.
  - Action değerleri: promote, upgrade_request, board_approve, board_reject, asset_lock, revision_upload, style_activate, force_rerender.
  - Tüm panel eylemleri buraya yazılır.
  - `GET /projects/{id}/curation.jsonl` dışa aktarımı, ileride projeye özel kalite modeli eğitmek içindir. Gerçek MiniMax H3 çıktılarından türeyen kararlar bu eğitim verisine girmez (Lisans §V.3).
- **Metrikler** (`GET /projects/{id}/metrics` ve panelde "Verimlilik" bölümü):
  - Kabul edilen çekim başına üretilen varyasyon sayısı (toplam ve sekans bazında).
  - Bitmiş film dakikası başına sanal maliyet.
  - Sekansların varyasyon ve harcama payları.
  - Eleme nedenlerinin dağılımı (quality_gate, not_top_k, failed).
  - Test turu başarı oranı ve ortalama düzeltme turu.
  - Kaba kurgudan finale kadar iyileştirilen çekim oranı.
- **Profesyonel dışa aktarma:**
  - CMX3600 EDL ve OpenTimelineIO JSON (kesme listesinden).
  - Çekim başına klasör paketi: final video, rafine adaylar, sesler ayrı stem olarak (diyalog, müzik, sfx) ve bir zip.
  - Uç nokta: `GET /projects/{id}/export?format=edl|otio|package`.

**Kabul:**
- Her panel eylemi bir kürasyon olayı üretir.
- Metrikler duman testi sonrası tutarlı sayılar verir.
- EDL dosyası tüm çekimleri doğru in/out değerleriyle içerir.
- Paket zip'i açılabilir ve stem'ler doğru sürededir.

**Bölüm B tamamlanma tanımı:** Faz 11–17 kabul kriterlerini sağlıyor; duman testi iki sekanslı, varlık ve stil kılavuzlu, storyboard onaylı bir senaryoyu baştan sona tamamen ücretsiz çalıştırıyor; README yeni akışı Türkçe anlatıyor.

---

# Bölüm C — Ücretli entegrasyonlar (Faz 18; yaz ama çalıştırma, yalnızca açık onayımla)

## Faz 18 — Canlı sağlayıcılar
- **`live/runpod_h3.py` (varsayılan platform):** MiniMax H3, RunPod Serverless'ta. Kilit `guard("runpod_api_key")`.
  - İş: `POST /v2/{uç nokta}/run` (iş akışı JSON'u, giriş görselleri base64, parametreler; dalganın bütün işleri birlikte, soğuk başlatma paylaşılsın), `GET /status/{id}`; çıktı yanıtta base64 (≤9 MB) ya da worker'ın S3'üne yüklenip imzalı adresle; her çıktı çıktı güvencesinden geçer (`live/h3_outputs.py`: Content-Length, worker'ın bildirdiği ve yerelde hesaplanan SHA-256, MinIO'daki kopyanın yeniden doğrulanması).
  - Bedel: RunPod'un bildirdiği çalışma süresi + worker'ın soğuk başlatması × GPU tipinin saniye ücreti (`pricing.yaml › serverless.runpod`); boşta bekleyen worker'lar dakikalık görevle son etkin projeye yazılır.
  - Korumalar (her yeni işten önce): uç nokta yalnızca izinli veri merkezlerinde (varsayılan CA-MTL-1/2/3; EUR-NO-1, EUR-IS-* ayarla; okunamazsa üretim yok); RunPod bakiyesi eşiğin altındaysa yeni iş başlamaz, panelde uyarı.
  - Uç nokta ayarı: RTX 5090 + RTX PRO 6000, yalnızca volume'un veri merkezi, en az 0 worker (flex), boşta bekleme ayarlanabilir (varsayılan 60 sn); panelden REST v2 ile uygulanır.
- **Bölge (her arka uç):** izinli liste (`app/core/regions.py`); ABD denizaşırı toprakları (PR, GU, VI, AS, MP, UM) ve AB en dış bölgeleri (Kanarya, GF, RE, GP, MQ, YT, MF, Azorlar, Madeira) her zaman yasaklı; konumu bilinmeyen asla seçilmez.
- **`live/vast_h3.py` (deneysel, varsayılan kapalı):** MiniMax H3, yönetmenin vast.ai'de kiraladığı GPU makinesinde. Bilinen sorunlar README'de. Arka uç SGLang (`/v1/videos`) ya da ComfyUI (`/upload/image`, `/prompt`, `/history`, `/view`; yönetmenin "Export (API)" iş akışları, `{{...}}` yer tutucularıyla). Sunucu `127.0.0.1`'dedir; erişim SSH tüneli ya da portalın kimlik doğrulamalı vekiliyle (açık port yok).
  - **Kompozisyon koşulla kontrol edilir** (aynı seed farklı çözünürlükte farklı kompozisyon verir). Çekim başına denetçiden: FL2VA (varsayılan; storyboard karesinin yazısız hâli ya da yönetmenin yüklediği kare her varyasyonun ilk karesi, test ve tam tur aynı kareden) ya da Ref2VA (karakter referans seti: durağan görsel, ifade sayfası, üç dönüş karesi; en çok 9; varsa ilk kare de). Ücretli modda storyboard karesi ComfyUI `keyframe.json` ya da Flux ile çizilir, kalite kapısından geçer (en çok 3 deneme); ilk kare kullanılamazsa FL2VA çekimi üretilmez.
  - Model CFG damıtılmış: negatif istem gönderilmez, stil kılavuzunun negatifleri pozitif kısıtlara çevrilir (`style/constraints.py`).
  - 4–15 sn ve 24 fps üretir (kısa çekim kesilir), kendi sesi atılır, video doğrudan puanlanır. Gönderilmiş iş yeniden denemede yeniden üretilmez.
  - Kilit: `guard("vast_api_key")`; portal belirteci (`H3_API_KEY`) isteğe bağlı.
- **`live/vast_box.py`, `live/vast_api.py` (makine yönetimi):** iş kaydı; kuyruk `H3_IDLE_STOP_MINUTES` (varsayılan 10) boşsa makine durdurulur; boşta geçen süre son etkin projenin bütçesine yazılır (Kill-Switch görür, bütçe yetmezse makine durur); isteğe bağlı otomatik başlatma; ücretsiz moda dönerken makine durdurulur.
  - **Yedek makine:** otomatik başlatma başarısız olursa (GPU başkasına kiralanmış) uygun bölgede aynı GPU tipinde, fiyat tavanı ve en az RAM/disk/bağlantı koşullarına uyan teklif aranır; `H3_REPLACE_MODE` ask ise panelde onay beklenir, auto ise tavan altında sormadan kiralanır. Yeni makine `vastai/base-image` + `vast/setup.sh` ile kurulur (ComfyUI 127.0.0.1'de, portal kimlik doğrulamalı), kimlik/adres/belirteç Ayarlar'a yazılır, yeni makine hazır olunca eski durdurulmuş makine silinir.
- **Storyboard görsel modeli:** yalnızca Apache 2.0 lisanslı modeller kurulur (Qwen-Image-2512 varsayılan, FLUX.1 [schnell]); FLUX.1 [dev] ticari olmayan lisanslıdır, yerel kurulmaz.
- **Lisans koruması (MiniMax H3 Community License):** makine AB, BK, G. Kore ya da ABD'deyse izin işaretlenmedikçe üretim başlamaz (§V.4); her klibin kaydına ve MP4 meta verisine model, sürüm, arka uç ve sunucu konumu yazılır; çıktılar başka modelleri geliştirmek için kullanılamaz (§V.3): kürasyon kaydının eğitim dışa aktarımından gerçek H3 çıktılarından türeyen kararlar çıkarılır, pakete `MINIMAX_H3.txt` eklenir. H3'ün kendi türevleri (ör. H3 LoRA) serbesttir.
- **`live/fal_video.py` (isteğe bağlı; anahtarı yoksa storyboard karesi yerel çizilir ve dudak senkronu atlanır):**
  - Flux LoRA (`fal-ai/flux-lora`) ile ilk kare, ardından Wan 2.2 (`fal-ai/wan/v2.2-a14b/image-to-video`) ya da Seedance (`bytedance/seedance-2.0/fast/image-to-video`).
  - Dudak senkronu: `fal-ai/sync-lipsync/v2/pro`.
  - 429 ve 5xx hataları TransientError olarak ele alınır. Uç nokta kimliklerini ve parametreleri fal.ai model sayfalarından doğrula.
- **`live/elevenlabs.py`:** `eleven_multilingual_v2`; duygu `stability` ve `style` değerlerine eşlenir.
- **`live/hermes_llm.py`:** LangGraph akışı: Send ile paralel extract, ardından validate, ardından en fazla 2 tur repair. Çıktı şeması kural tabanlı ayrıştırıcıyla aynıdır.
- **Önce görsel, sonra video (fal yolu, Seedance çekimleri):** ücretli modda kalite kontrolü önce ilk kare üzerinde yapılır. Video yalnızca eşiği geçen ilk kareler için üretilir.
- **Karakter referansı:** karakterin durağan referans görseli varsa ücretli ilk kare ve storyboard karesi Flux Kontext (`fal-ai/flux-pro/kontext`) ile o görselden çizilir; LoRA varsa LoRA kazanır. `paid_audio: false` proje ayarı (fal.ai denemesi) ücretli modda da sesi yerel tutar ve dudak senkronunu çağırmaz.
- **Prompt çevirisi:** görüntü modelleri İngilizce istemi izler. Ücretli modda bestecinin çıktısındaki Türkçe metin, sağlayıcıya gitmeden yerel ve ücretsiz bir modelle (opus-mt-tr-en) İngilizceye çevrilir; karakter adları korunur, görsel özet değişmez.
- **Bölüm B ile bağlantılar:**
  - Storyboard karesi için Flux (Faz 14).
  - Previs rehber videosundan video-to-video stil aktarımı (Faz 15).
  - Karakter dönüş videosu üretimi (Faz 12).
- **Sonraki adımlar:**
  - Düşük skor veya anomali geldiğinde promptu revize eden döngüsel düzeltme grafiği. Sayısal parametreleri kod, metni LLM değiştirir.
  - Gerçek kalite skoru: OpenCLIP ile stil, InsightFace/ArcFace ile yüz; birleşik skor.
  - Pinecone ile karakter vektörleri ve iki katmanlı önbellek (süreç belleği + Redis, anahtarda embedding_version).
  - Kürasyon kayıtlarından (Faz 17) projeye özel kalite modeli eğitimi.
  - Next.js paneline geçiş (yapıldı: bkz. Teknoloji yığını; varyasyon karşılaştırma görünümüyle).

---

## Çalışma biçimin
- Fazları sırayla uygula. Her fazın başında kısa bir plan yaz.
- Her fazın sonunda testleri çalıştır, sonucu raporla ve commit at.
- Belirsizlikte en basit ve ücretsiz çözümü seç; varsayımını yaz.
- Para harcayabilecek, veri silebilecek ya da bu belgedeki bir kuralı değiştirecek her adımdan önce dur ve bana sor.