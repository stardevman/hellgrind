# Hell Grind — AI Animasyon Fabrikası

Yönetmenin "Sistem Küratörü" olduğu, otonom bir AI animasyon üretim hattı.

Bu repo **Kademe 1** durumundadır: tüm altyapı bilgisayarında çalışır ve **hiçbir ücretli API çağrılmaz** (`DRY_RUN=1`). Video üretimi yerine hazır örnek videolar kullanılır, ama bütçe, kalite kontrolü, Stateless Re-render ve montaj mantığının tamamı gerçektir.

## Kademe 1'de neler çalışıyor

| Bileşen | Durum |
| --- | --- |
| PostgreSQL şeması (projeler, karakterler, sahneler, varyasyonlar, ses kanalları, harcama defteri) | ✅ Gerçek |
| FastAPI kontrol düzlemi + Celery kuyrukları (gen / post / control) | ✅ Gerçek |
| Kill-Switch: atomik bütçe rezervasyonu, dondurma, görev iptali, hız bekçisi | ✅ Gerçek |
| Kademeli İterasyon: test turu → tam tur dalgaları → erken durma | ✅ Gerçek |
| Quality Gate: siyah/donmuş kare tespiti (FFmpeg), imha/karantina, en iyi 3 | ✅ Gerçek (CLIP skoru sahte) |
| Stateless Re-render: yalnızca değişen aşama yeniden çalışır | ✅ Gerçek |
| FFmpeg muksing: diyalog + müzik, müziği diyalog altında kısma, ses normalizasyonu | ✅ Gerçek |
| Video üretimi (fal.ai / Seedance) | 🟡 Sahte (örnek videolar) |
| Ses (ElevenLabs), dudak senkronu | 🟡 Sahte |
| Senaryo ayrıştırma (LangGraph), Pinecone, Next.js paneli | ⏳ Kademe 2+ |

## Gereksinimler

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows, macOS veya Linux)
- [Git](https://git-scm.com/downloads)
- Bir GitHub hesabı

## 1. GitHub'a yükle

1. github.com'da sağ üstteki **+** → **New repository**.
2. Ad: `hellgrind`, görünürlük: **Private**. README, .gitignore veya lisans **ekleme** (boş repo olsun).
3. Bu klasörde terminal aç ve çalıştır (`KULLANICI_ADIN` kısmını değiştir):

```bash
git init
git add .
git commit -m "Kademe 1: yerel altyapı, Kill-Switch, Stateless Re-render"
git branch -M main
git remote add origin https://github.com/KULLANICI_ADIN/hellgrind.git
git push -u origin main
```

Her push'ta GitHub Actions birim testleri otomatik çalıştırır (Actions sekmesi). Private repolarda aylık ücretsiz dakika kotası bu testler için fazlasıyla yeterli.

## 2. Çalıştır

```bash
cp .env.example .env                 # Windows PowerShell: Copy-Item .env.example .env
docker compose up -d --build         # ilk derleme birkaç dakika sürer
docker compose run --rm api python -m scripts.make_samples   # örnek videolar (bir kez)
docker compose exec api python -m scripts.smoke              # uçtan uca test
```

`make` kuruluysa kısaca: `make up`, `make samples`, `make smoke`.

Başarılı bir duman testinin sonu şöyle görünür:

```text
TÜM KONTROLLER GEÇTİ ✅
```

## 3. Ne nereye bakılır

| Adres | Ne |
| --- | --- |
| http://localhost:8000/ui | **Stüdyo paneli**: adım adım proje, sahne, ses, render, sonuç ve bütçe (ilk açılışta rehber turu) |
| http://localhost:8000/docs | API (tarayıcıdan tüm uç noktaları deneyebilirsin) |
| http://localhost:5555 | Flower: Celery kuyrukları ve görevler |
| http://localhost:9001 | MinIO konsolu (kullanıcı `minio`, şifre `minio12345`): üretilen videolar |
| `docker compose logs -f worker` | Üretim günlüğü (test turu, dalgalar, Kill-Switch) |

## 4. Stüdyo paneliyle deneme

http://localhost:8000/ui adresini aç. İlk açılışta kısa bir rehber turu başlar (sonra sağ üstteki **❓ Rehber** ile tekrar açılır). En hızlı yol **✨ Örnek proje oluştur**: sahne ve diyalogu hazır bir proje kurar, render'ı başlatır ve sonucu 4. adımda gösterir. Panel aşağıdaki API ile aynı uç noktaları kullanır; gelişmiş ayarlar kapalı bölümlerde durur.

## 5. Elle deneme (API /docs üzerinden)

1. `POST /api/projects` → `{"title": "Deneme", "budget_limit_usd": 10}`
2. `POST /api/projects/{id}/scenes` → `{"scene_code": "S001_SH01", "seq_order": 1}`
3. `PUT /api/scenes/{id}/audio` → `{"kind": "dialogue", "text": "Merhaba"}` (render otomatik başlar)
4. `GET /api/scenes/{id}` ile durumu izle; `done` olunca `GET /api/scenes/{id}/final-url`
5. `GET /api/projects/{id}/budget` → harcama defteri ve olaylar
6. `POST /api/projects/{id}/killswitch` → `{"action": "freeze"}` ile elle dondur, `"resume"` ile devam ettir

Proje oluştururken `settings` ile varsayılanları değiştirebilirsin, örn. `{"full_n": 20, "clip_threshold": 0.88}`. Tüm ayarlar: `backend/app/config.py` → `DEFAULT_PROJECT_SETTINGS`.

## Kademe 1 kontrol listesi

- [ ] Docker ile tüm servisler ayakta (`docker compose ps`)
- [ ] Örnek medya oluşturuldu (`samples/` klasörü)
- [ ] Duman testi geçti
- [ ] Kill-Switch'i elle denedim (dondur → render 423 döndü → devam ettir)
- [ ] `DRY_RUN_FAIL_RATE=0.2` ile yeniden deneme mantığını gözlemledim
- [ ] fal.ai, ElevenLabs ve diğer sağlayıcılarda hesap açıp **aylık harcama limitini** düşük ayarladım
- [ ] Repo GitHub'da ve Actions testleri yeşil

## Proje yapısı

```text
db/init/001_core.sql          PostgreSQL şeması (ilk açılışta otomatik uygulanır)
backend/pricing.yaml          Sağlayıcı birim fiyatları (Kill-Switch tahminleri)
backend/app/
  core/                       hashing + Stateless Re-render planlayıcısı
  budget/                     Kill-Switch ve fiyatlandırma
  engine/                     sağlayıcılar (dry-run) + Kademeli İterasyon
  quality/                    Quality Gate + FFmpeg anomali tespiti
  post/                       ses, dudak senkronu, FFmpeg muksing
  api/                        FastAPI uç noktaları
  worker/                     Celery uygulaması ve görevleri
backend/scripts/              make_samples, smoke
backend/tests/                birim testleri
```

## Sorun giderme

- **Şema değişikliği uygulanmadı:** PostgreSQL `db/init` dosyalarını yalnızca ilk açılışta çalıştırır. Her şeyi sıfırla: `docker compose down -v && docker compose up -d --build` (ardından `make_samples` adımını tekrarla).
- **"Örnek video yok" hatası:** `make_samples` adımını çalıştırıp sahneyi yeniden render et.
- **Port meşgul (5432, 6379, 8000...):** Bilgisayarında aynı portu kullanan bir servis var; onu durdur ya da `docker-compose.yml` içindeki sol taraftaki port numarasını değiştir.
- **Sahne `needs_review` durumunda kaldı:** Sahte skorlar eşiği geçemedi; `GET /api/scenes/{id}/variations` ile skorlara bak. `DRY_RUN_SCORE_MEAN` değerini artırarak davranışı değiştirebilirsin.

## Sonraki adım: Kademe 2 (yaklaşık 10–30 USD)

1. `engine/providers.py`: fal.ai Flux LoRA + Wan 2.2 sağlayıcısı
2. `quality/gate.py`: gerçek CLIP (OpenCLIP) ve yüz (InsightFace) skorları
3. `post/audio.py`: ElevenLabs; `post/pipeline.py`: fal lip-sync
4. Tek karakter için LoRA eğitimi, 3–5 çekimlik mikro pilot
5. Hermes: senaryo ayrıştırma (LangGraph)
