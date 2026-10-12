from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://hg:hg@postgres:5432/hellgrind"
    redis_url: str = "redis://redis:6379/0"

    s3_endpoint_url: str | None = "http://minio:9000"
    s3_access_key: str = "minio"
    s3_secret_key: str = "minio12345"
    s3_bucket: str = "hellgrind-media"
    s3_region: str = "us-east-1"

    samples_dir: str = "/samples"
    work_dir: str = "/tmp/hellgrind"
    pricing_file: str = "/app/pricing.yaml"
    panel_web_dir: str = "/srv/panel"        # Next.js panelinin statik dışa aktarımı (imajda derlenir; yoksa yedek sayfa)

    dry_run: bool = True
    dry_run_fail_rate: float = 0.0
    dry_run_score_mean: float = 0.89
    dry_run_score_sd: float = 0.05
    dry_run_defect_rate: float = 0.12     # bilerek bozuk (siyah/donmuş) klip oranı
    local_render_fps: int = 24
    tts_engine: str = "espeak"            # espeak (ücretsiz, Türkçe) | tone | elevenlabs (ücretli; ücretsiz modda espeak kullanılır)
    hermes_mode: str = "rules"            # rules (ücretsiz) | llm (ücretli, ANTHROPIC_API_KEY ister)

    # Kademe 2 anahtarları: DRY_RUN=1 iken hiçbiri okunmaz
    fal_key: str = ""
    anthropic_api_key: str = ""
    elevenlabs_api_key: str = ""
    hermes_model: str = "claude-sonnet-5-5"
    # Sahne istemi (referans modu): llm = yönetmen tarzı istemi Hermes modeli yazar (ücretli, ANTHROPIC_API_KEY; anahtar
    # yoksa ya da ücretsiz modda kurallara düşer, planda uyarı) | rules = kural tabanlı istem
    prompt_writer: str = "llm"
    # Ücretli model ve uç nokta kimlikleri (panelin Ayarlar sayfasından da düzenlenir; fal.ai model sayfalarıyla doğrula)
    fal_flux_endpoint: str = "fal-ai/flux-lora"
    fal_wan_endpoint: str = "fal-ai/wan/v2.2-a14b/image-to-video"
    fal_seedance_endpoint: str = "bytedance/seedance-2.0/fast/image-to-video"
    fal_lipsync_endpoint: str = "fal-ai/sync-lipsync/v2/pro"
    fal_wan_v2v_endpoint: str = "fal-ai/wan/v2.2-a14b/video-to-video"
    fal_kontext_endpoint: str = "fal-ai/flux-pro/kontext"     # karakterin referans görseliyle kare (görsel başına)
    # Video motorları fal.ai'de (Ekim 2026): MiniMax H3 Max taslak, Seedance 2.5 final (karşılaştırma testi). /us/
    # varyantları kullanılmaz. Şemalar: https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<uç nokta>
    fal_h3max_i2v_endpoint: str = "minimax/h3-max/image-to-video"
    fal_h3max_ref_endpoint: str = "minimax/h3-max/reference-to-video"
    fal_seedance25_i2v_endpoint: str = "bytedance/seedance-2.5/image-to-video"
    fal_seedance25_ref_endpoint: str = "bytedance/seedance-2.5/reference-to-video"
    fal_seedance25_draft_complete_endpoint: str = "bytedance/seedance-2.5/draft/complete"
    video_draft_engine: str = "h3max"          # taslak: yeni çekimlerin motoru (proje ayarı draft_engine önce gelir)
    video_final_engine: str = "seedance25"     # final: "Seedance 2.5 ile yükselt" (proje ayarı final_engine önce gelir)
    h3max_resolution: str = "480P"             # 480P (varsayılan, test aşaması: en ucuz) | 768P | 1080P
    h3max_prompt_expansion: str = "disabled"   # disabled | balanced | quality (zorunlu alan)
    seedance25_draft: bool = True              # taslak (draft: true): 480p önizleme + draft_id, 7 gün içinde 1080p'ye tamamlanır
    seedance25_resolution: str = "720p"        # 480p | 720p | 1080p; yalnızca taslak kapalıyken kullanılır
    # Upscale (onaylanmış klipleri 1080p'ye çıkaran ayrı adım; kaynak fps korunur)
    upscale_endpoint: str = "fal-ai/bytedance-upscaler/upscale/video"
    upscale_target: str = "1080p"
    upscale_tier: str = "standard"             # bytedance: fast | standard | pro (pro 10 kat fiyat)
    seedance25_audio: bool = False             # modelin ürettiği ses: filmde kullanılmaz; fiyatı değiştirmez
    # fal.ai videosunun girdisi: reference (varsayılan) = yalnızca varlıkların onaylı referans görselleri + senaryo; hiçbir
    # kare ya da görsel üretilmez (storyboard çizimi ve kare kapıları atlanır). keyframe = storyboard karesinden (Qwen).
    video_input_mode: str = "reference"
    # Sahnenin üretimi: whole_scene (varsayılan) = sahnenin bütün çekimleri tek istekte ("Shot 1 (0-3s) …"), motor
    # sınırını (H3 Max 15 sn, Seedance 2.5 30 sn) aşan sahne çekim sınırlarından parçalara bölünür | per_shot = her çekim ayrı
    scene_render_mode: str = "whole_scene"
    # Kare çizimi (storyboard ilk karesi, zincir, kesme, referans görselleri): fal (varsayılan; RunPod silindi) | runpod
    keyframe_platform: str = "fal"
    fal_qwen_image_endpoint: str = "fal-ai/qwen-image-2512"                  # metinden kare (Qwen-Image-2512)
    fal_qwen_edit_endpoint: str = "fal-ai/qwen-image-edit-2511"              # karakter referansından ve zincir kareleri
    fal_qwen_edit_lora_endpoint: str = "fal-ai/qwen-image-edit-2511/lora"    # Multiple Angles LoRA (loras alanıyla)
    # MiniMax H3: yönetmenin kendi GPU sunucusu (vast.ai). Arka uç SGLang (/v1/videos) ya da ComfyUI (/prompt, /history,
    # /view; 127.0.0.1'de, SSH tüneli ya da portalın kimlik doğrulamalı vekili üzerinden). Ücretli kilidin anahtarı vast.ai
    # API anahtarıdır (makineyi o faturalar; boşta durdurma ve otomatik başlatma da onunla yapılır). Portal belirteci
    # (OPEN_BUTTON_TOKEN ya da WEB_PASSWORD) isteğe bağlıdır: SSH tünelinde gerekmez.
    # Kendi barındırdığımız MiniMax H3'ün platformu: kapali (varsayılan; RunPod uç noktası ve volume silindi, video fal.ai'de)
    # | runpod (Serverless, Network Volume) | vast (deneysel). Kapalıyken RunPod/vast.ai'ye hiçbir çağrı yapılmaz.
    h3_platform: str = "kapali"
    runpod_api_key: str = ""
    runpod_endpoint_id: str = ""       # Serverless uç noktası (ComfyUI worker'ı; backend/runpod/worker)
    runpod_network_volume_id: str = ""
    runpod_datacenter: str = "CA-MTL-1"   # Network Volume'un veri merkezi; uç nokta yalnızca bu merkezde çalışır
    runpod_gpu_types: str = "RTX 5090,RTX PRO 6000"
    runpod_gpu_pools: str = "auto"        # uç noktaya yazılan GPU havuzları: auto (GPU tiplerinden) ya da havuz kimlikleri
    runpod_idle_timeout_s: int = 60    # worker son işten sonra bu kadar bekler (faturalanır), sonra kapanır
    runpod_review_idle_s: int = 300    # storyboard şeridi incelemesi: kare işinden sonra geçici boşta bekleme (0: kapalı)
    runpod_min_balance_usd: float = 10.0   # RunPod bakiyesi bunun altındaysa yeni iş başlamaz
    runpod_max_pending_jobs: int = 6       # bir projede RunPod'da aynı anda en çok bekleyen/çalışan iş; fazlası yer bekler
    runpod_workers_max: str = "keep"       # uç noktanın en çok worker'ı: keep (değiştirme) | 1 | 2 | 3
    # H3 DiT dosyası: fp8_scaled (cu128 torch'lu worker; varsayılan) | int8_convrot (Comfy-Org'un önerisi; cu130 torch ister)
    h3_unet_variant: str = "fp8_scaled"
    # Karakterli çekimin ilk karesi: qwen_edit (varsayılan; Qwen-Image-Edit-2511, karakterin onaylı referansından,
    # edit_keyframe.json) | qwen (Qwen-Image, yalnızca metinden; kimlik kayabilir). Karaktersiz çekim her zaman qwen.
    keyframe_character_provider: str = "qwen_edit"
    qwen_edit_mode: str = "full"        # Qwen-Image-Edit: full (40 adım, cfg 4) | medium (20 adım, cfg 4) | lightning (4, cfg 1)
    # Qwen-Image-Edit-2511 Multiple Angles LoRA (fal, Apache-2.0): kesmede açı değişimi; dosya volume'da models/loras/
    qwen_edit_angles: str = "off"       # on: açı değişen kesme karelerinde açılır (dosya yüklenince açın)
    qwen_edit_angles_lora: str = "qwen-image-edit-2511-multiple-angles-lora.safetensors"
    # fal.ai'de aynı LoRA (loras[].path bir URL ister): Hugging Face'teki dosya (Apache-2.0)
    qwen_edit_angles_lora_url: str = ("https://huggingface.co/fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA/resolve/main/"
                                      "qwen-image-edit-2511-multiple-angles-lora.safetensors")
    qwen_edit_angles_strength: float = 0.9
    qwen_edit_angles_mode: str = "full"     # açı karesi: full | medium | lightning (Lightning ile uyumu ilk koşuda sınanacak)
    reference_fal_fallback: bool = False   # referans görseli: Qwen-Image yoksa fal.ai Flux'a düş (FLUX.1 [dev] ticari değil)
    # İlk kareyi karakter referansıyla fal.ai'de çizme kancası: off (varsayılan) | flux_kontext (Qwen'in önüne geçer)
    h3_keyframe_reference: str = "off"
    vast_api_key: str = ""
    vast_instance_id: str = ""
    h3_api_key: str = ""               # portal belirteci (Authorization: Bearer); boşsa gönderilmez
    h3_backend: str = "comfyui"        # comfyui | sglang (deneysel)
    h3_endpoint_url: str = ""          # FL2VA (SGLang) ya da ComfyUI adresi
    h3_ref_endpoint_url: str = ""      # Ref2VA sunucusu (SGLang); ComfyUI'de boşsa aynı adres kullanılır
    h3_comfy_workflow_dir: str = "/config/comfy"   # ComfyUI iş akışları (API biçimi, yer tutuculu): t2v, i2v, r2v, keyframe
    h3_model: str = "MiniMaxAI/MiniMax-H3"
    h3_model_revision: str = ""        # kayda ve klip meta verisine yazılır (ör. "fl2va pruned int8 convrot + turbo v4")
    h3_steps: int = 9                  # sunucudaki kurulumla aynı olmalı: SGLang turbo LoRA 9 (8 değerlendirme), temel 20-50
    h3_timeout_s: int = 3600           # tek işin (kuyrukta bekleme dahil) en uzun süresi
    h3_idle_stop_minutes: int = 10     # kuyruk bu kadar dakika boşsa makine durdurulur (0: kapalı)
    h3_auto_start: bool = False        # iş gelince durmuş makine başlatılır
    h3_start_timeout_s: int = 900      # otomatik başlatmada sunucunun hazır olmasının beklendiği en uzun süre
    h3_allow_nordic: bool = False      # izinli bölgelere Kuzey'i ekle: RunPod EUR-NO-1, EUR-IS-*; vast.ai NO, IS (app/core/regions.py)
    h3_tls_mode: str = "verify"        # verify | pin (ilk başarılı bağlantıda sertifikayı sabitle; yeni makine) | off
    # Yedek makine: otomatik başlatma başarısız olursa (durdurulan makinenin GPU'su başkasına kiralanmış olabilir) uygun
    # bölgelerde aynı GPU tipinde yeni bir teklif bulunur, kiralanır, setup.sh ile kurulur; eski makine silinir.
    h3_replace_mode: str = "ask"       # off | ask (her zaman sor) | auto (fiyat tavanı altındaysa sormadan kirala)
    h3_max_dph: float = 0.95           # yeni makinenin saatlik fiyat tavanı ($/sa, vast.ai dph_total)
    h3_min_ram_gb: int = 128           # ilk gerçek deneme: yalnızca H3 ~94 GB (sayfa önbelleği dahil); Qwen-Image +~30 GB
    h3_min_disk_gb: int = 200          # H3 int8 FL2VA + Ref2VA + metin kodlayıcı (~75 GB) + görsel modeli (~30 GB) + ortam
    h3_min_inet_mbps: int = 1000       # modeller ~105 GB: 1 Gbps'te ~15 dk, yavaş bağlantıda saatler
    h3_gpu_name: str = ""              # boşsa eski makinenin GPU'su (ör. "RTX 5090")
    h3_box_image: str = "vastai/base-image:@vastai-automatic-tag"
    h3_setup_script_url: str = ""      # boşsa setup.sh isteğe gömülür (gzip + base64)
    h3_image_model: str = "qwen-image-2512"   # storyboard ilk karesi: qwen-image-2512 | flux-schnell | none (Apache 2.0)
    h3_setup_timeout_s: int = 5400     # yeni makinede kurulumun (model indirme dahil) en uzun süresi
    elevenlabs_model: str = "eleven_multilingual_v2"
    # Panelin yazdığı .env (konteynerlere bağlanır). Boşsa panelden ayar düzenleme kapalıdır (testler, CI).
    config_env_file: str = ""
    s3_public_endpoint_url: str | None = "http://localhost:9000"   # tarayıcının MinIO'ya eriştiği adres

    purge_mode: str = "quarantine"   # hard | quarantine
    gen_concurrency: int = 3

    # Kalite skoru: auto = ücretsiz modda sahte (seed'e bağlı), ücretli modda gerçek (OpenCLIP + YuNet/SFace);
    # dry = her zaman sahte (ücretli modda üretim başlatılmaz); real = her zaman gerçek (ücretsiz modda da denenebilir)
    quality_scorer: str = "auto"
    quality_model_dir: str = "/models"
    score_face_weight: float = 0.4   # birleşik skorda yüz kimliğinin payı (yüz görünüyorsa)
    # Prompt çevirisi (Türkçe -> İngilizce, yerel opus-mt-tr-en, ücretsiz): auto = yalnızca ücretli modda, sağlayıcıya
    # giden prompt'a; on = her zaman (ücretsiz modda prompt'ları görmek için); off = hiç
    prompt_translation: str = "auto"


settings = Settings()

# Proje oluşturulurken verilmeyen ayarlar bu değerleri alır.
DEFAULT_PROJECT_SETTINGS: dict = {
    "clip_threshold": 0.85,        # Quality Gate eşiği
    "identity_threshold": 0.60,
    "look_threshold": 0.70,        # insan olmayan karakter: OpenCLIP görüntü benzerliği (kare/klip ile referans), alt sınır
    "look_dino_threshold": 0.78,   # insan olmayan karakter: DINOv2 (kırpımlar), alt sınır (zincir koşusu: doğru Bolt 0,80-0,91)
    "look_review_margin": 0.05,    # eşiğin bu kadar üstüne kadar: geçer ama "inceleme önerilir"
    "state_threshold": 0.5,        # durum sondası (OpenCLIP, karakter kırpımı): istenen durumun olasılığı bunun altındaysa yeniden dene
    "continuity_threshold": 0.70,  # zincir karesi (continuous): önceki kareyle mekân benzerliği (karakter bölgesi dışı, DINOv2)
    "cut_continuity_threshold": 0.35,   # kesme karesi mekânın geniş görüntüsünden yeni kamerayla çizilir: arka plan
                                        # benzerliği düşer (sentetik %60 yakınlaşma 0,42); yalnızca bambaşka mekânı eler
    "transition_threshold": 0.80,  # FL2VA last_frame: klibin son karesi ile sonraki çekimin ilk karesi
    "arrival_review_threshold": 0.85,   # varış skoru (son kare -> K(n+1) ya da Kn_end) bunun altındaysa "inceleme önerilir"
    "chain_end_frame": True,       # kesme öncesi çekimde video kendi son karesine (Kn_end) gider; kapalıysa son kare serbest
    "seam_mode": "keyframe",       # keyframe: continuous çekimler anahtar karelerle dikilir; exact: N+1'in ilk karesi N'nin
                                   # kazanan klibinin gerçek son karesi (sıralı üretim)
    "seam_crossfade_frames": 0,    # continuous dikişlerde filmde kaç karelik geçiş (0 kapalı; 3-4 önerilir)
    "audio": "on",                 # off: H3 sesi üretilmez (ses VAE'si yüklenmez, video sessiz); film sesi Hell Grind'den
    "qwen_edit_mode": None,        # karakterli ilk kare: None = Ayarlar'daki QWEN_EDIT_MODE; full | medium | lightning    # kimlik kapısı: yüz beklenen çekimde karakter benzerliği bunun altındaysa havuza giremez
    "excellent_threshold": 0.92,   # tam turda erken durma için "mükemmel" eşiği
    "min_pass_rate": 0.3,          # test turunun geçmesi için gereken oran
    "probe_n": 10,                 # test turu varyasyon sayısı
    "full_n": 50,                  # tam tur üst sınırı
    "wave_size": 10,               # tam tur dalga büyüklüğü
    "refined_k": 3,                # Rafine Havuz boyutu
    "max_correction_rounds": 2,    # test turu başarısızsa yeniden deneme sayısı
    "probe_resolution": "480p",
    "full_resolution": "720p",
    "soft_budget_ratio": 0.8,      # bu orana gelince yeni tam dalga başlatılmaz
    "max_spend_per_10min": 20.0,   # hız bekçisi: 10 dakikada bundan fazla harcanırsa dondur
    "loudness_lufs": -23.0,
    "lint_strictness": "block",    # üretim öncesi referans kontrolü: off | warn | block
    "asset_min_status": "locked",  # çekimdeki varlıkların ulaşması gereken en az durum (still_approved | motion_tested | locked)
    "require_board_approval": False,   # açıksa storyboard karesi onaylanmadan görüntü üretilmez
    "paid_audio": True,            # false: ücretli modda da ses yereldir (espeak) ve dudak senkronu çağrılmaz
    "draft_engine": None,          # taslak video motoru: None = Ayarlar'daki VIDEO_DRAFT_ENGINE (h3max)
    "final_engine": None,          # final video motoru ("yükselt"): None = Ayarlar'daki VIDEO_FINAL_ENGINE (seedance25)
    "require_plan_approval": None, # render'dan önce kuru çalıştırma planı onaylanmalı mı; None = ücretli modda evet
    "h3max_resolution": None,      # proje: H3 Max çözünürlüğü; None = Ayarlar'daki H3MAX_RESOLUTION (480P)
    "seedance25_draft": None,      # proje: Seedance 2.5 taslak (480p); None = Ayarlar'daki SEEDANCE25_DRAFT (açık)
    "seedance25_resolution": None, # proje: taslak kapalıyken Seedance çözünürlüğü; None = Ayarlar'daki SEEDANCE25_RESOLUTION
    "fal_clip_attempts": 2,        # fal motorları (H3 Max, Seedance 2.5) çekim başına tek klip üretir; kalite kapısından
                                   # kalırsa en çok bu kadar klip (her biri ayrı ücretli)
}


def project_settings(raw: dict | None) -> dict:
    """Projenin kendi ayarları, verilmeyenler için varsayılanlarla birleştirilmiş."""
    return {**DEFAULT_PROJECT_SETTINGS, **(raw or {})}
