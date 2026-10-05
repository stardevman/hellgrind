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
    # Ücretli model ve uç nokta kimlikleri (panelin Ayarlar sayfasından da düzenlenir; fal.ai model sayfalarıyla doğrula)
    fal_flux_endpoint: str = "fal-ai/flux-lora"
    fal_wan_endpoint: str = "fal-ai/wan/v2.2-a14b/image-to-video"
    fal_seedance_endpoint: str = "bytedance/seedance-2.0/fast/image-to-video"
    fal_lipsync_endpoint: str = "fal-ai/sync-lipsync/v2/pro"
    fal_wan_v2v_endpoint: str = "fal-ai/wan/v2.2-a14b/video-to-video"
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
}


def project_settings(raw: dict | None) -> dict:
    """Projenin kendi ayarları, verilmeyenler için varsayılanlarla birleştirilmiş."""
    return {**DEFAULT_PROJECT_SETTINGS, **(raw or {})}
