# Hell Grind — AI Animasyon Fabrikası

Yönetmenin "Sistem Küratörü" olduğu, otonom bir AI animasyon üretim hattı.

![Kontrol paneli](docs/panel.png)

## Ücretsiz mod (varsayılan)

`DRY_RUN=1` iken **hiçbir ücretli servis çağrılmaz**; API anahtarı girsen bile kullanılmaz. Ücretli kodun her giriş noktası `app/live/guard()` ile korunur ve bu koruma testlerle doğrulanır.

| Parça | Ücretsiz modda | Ücretli modda (Kademe 2) |
| --- | --- | --- |
| Senaryo ayrıştırma | Kural tabanlı Türkçe ayrıştırıcı | LangGraph + Claude |
| Görüntü üretimi | Yerel prosedürel render: duygu, kamera, karakterler ve seed'e göre değişen sahneler | fal.ai Flux LoRA → Wan 2.2 / Seedance |
| Kalite kontrolü | Siyah ve donmuş kare tespiti (gerçek, FFmpeg) + sahte skor | + CLIP ve yüz benzerliği |
| Diyalog sesi | espeak-ng ile Türkçe konuşma | ElevenLabs |
| Müzik | Duyguya göre akor seçen prosedürel fon müziği | Lisanslı dosya |
| Dudak senkronu | Atlanır | fal lip-sync |
| Bütçe | Gerçek sağlayıcı fiyatlarıyla **sanal** muhasebe, Kill-Switch gerçek | Gerçek harcama |
| Kademeli iterasyon, Rafine Havuz, Stateless Re-render, montaj, film birleştirme | Gerçek | Gerçek |

Yerel render bir yapay zekâ modeli değildir; amacı, tüm hattı ve paneli gerçek görüntülerle, para harcamadan denemektir.

## Kurulum

Gereksinimler: [Docker Desktop](https://www.docker.com/products/docker-desktop/) ve Git.

```bash
git clone https://github.com/stardevman/hellgrind.git
cd hellgrind
cp .env.example .env                 # Windows PowerShell: Copy-Item .env.example .env
docker compose up -d --build
```

Tarayıcıda **http://localhost:8000** adresini aç. Panel örnek bir senaryoyla açılır; "Senaryoyu işle ve üret" düğmesine bas. Hızlı önizleme modunda 4 çekimlik örnek film birkaç dakikada hazır olur.

### Önceki sürümden güncelliyorsan

```bash
cp .env.example .env                 # yeni ayarlar var; eski .env'i bununla değiştir
docker compose up -d --build         # veritabanı silinmez, yeni sütunlar otomatik eklenir
```

## Panelde neler var

- **Film şeridi:** her sahne için üretilen tüm varyasyonlar. Küçük kareler test turu, büyükler tam tur. Yeşil çerçeveli ve numaralı olanlar Rafine Havuz; kırmızı çizgili olanlar kalite kontrolünde elendi; soluk olanlar havuza giremedi. Bir kareye tıklayınca o varyasyonu (elenmiş olsa bile, karantinadan) izlersin.
- **Sonuç:** sesi ve müziği eklenmiş sahne.
- **Rafine havuz:** "Bunu kullan" ile başka bir adayı seçersin. Yalnızca ses ve montaj yeniden yapılır.
- **Müziği değiştir:** yalnızca montaj yeniden yapılır; görüntü motoru çağrılmaz ve bütçe değişmez. Stateless Re-render'ı burada canlı görürsün.
- **Bütçe rayı ve "Üretimi durdur":** Kill-Switch. Sanal limit dolunca üretim kendiliğinden durur.
- **Film:** tamamlanan sahneleri tek videoda birleştirir; alttaki renkli kesme şeridine tıklayınca o sahneye atlar.

Kendi senaryonu yazarken standart biçimi kullan: `İÇ.` / `DIŞ.` ile başlayan sahne başlıkları, büyük harfle karakter adı, altında isteğe bağlı `(duygu)` ve replik.

## Test

```bash
make test        # docker compose run --rm -v ./db:/db:ro api python -m pytest -q
make smoke       # docker compose exec api python -m scripts.smoke  (çalışan yığına karşı, uçtan uca, ücretsiz)
```

`make test` iki katmanlıdır:

- **Birim testleri** servis gerektirmez (FFmpeg ve espeak-ng yeter): yeniden render planlayıcısı, Türkçe ayrıştırıcı, prosedürel render, anomali tespiti, ses, müzik, miks, ücretli kilit.
- **Entegrasyon testleri** PostgreSQL, Redis ve MinIO'yu kullanır: Kill-Switch, iterasyon, ses/montaj aşamaları, film birleştirme, API ve örnek filmin uçtan uca üretimi. Bu testler geliştirme verilerine **dokunmaz**: ayrı `hellgrind_test` veritabanını ve `hellgrind-test` kovasını kullanır (yoksa kendisi kurar), Celery kuyruğuna görev bırakmaz. Servisler yoksa (ör. GitHub Actions) atlanır.

Duman testi; senaryodan filme tüm akışı, yalnızca müzik değişince görüntü motorunun çağrılmadığını, küratör seçimini, film birleştirmeyi, Kill-Switch'i ve ücretli çağrıların engellendiğini çalışan yığın üzerinde doğrular. Her çalıştırmada panelde "Smoke" adlı iki proje oluşur; istersen silebilirsin. GitHub Actions her push'ta servis gerektirmeyen testleri çalıştırır.

## Adresler

| Adres | Ne |
| --- | --- |
| http://localhost:8000 | Kontrol paneli |
| http://localhost:8000/docs | API |
| http://localhost:5555 | Flower: Celery kuyrukları |
| http://localhost:9001 | MinIO (kullanıcı `minio`, şifre `minio12345`): tüm dosyalar |

## Ayarlar (.env)

| Değişken | Etkisi |
| --- | --- |
| `DRY_RUN` | 1 = ücretsiz mod. Ücretli denemeye karar verene kadar değiştirme. |
| `LOCAL_RENDER_FPS` | Düşürürsen (ör. 12) yerel render hızlanır |
| `GEN_CONCURRENCY` | Aynı anda kaç varyasyon üretilir; bilgisayar zorlanırsa 2 yap |
| `DRY_RUN_DEFECT_RATE` | Bilerek bozuk üretilen klip oranı (kalite kontrolünü görmek için) |
| `TTS_ENGINE` | `espeak` Türkçe konuşma, `tone` yapay ton |
| `PURGE_MODE` | `quarantine` elenenleri panelde izlenebilir tutar, `hard` siler |

## Proje yapısı

```text
db/init/001_core.sql        İlk şema (PostgreSQL ilk açılışta uygular)
backend/migrations/         Sonraki şema değişiklikleri (API açılışta uygular)
backend/pricing.yaml        Sağlayıcı birim fiyatları (sanal bütçe ve Kill-Switch)
backend/app/
  hermes/                   Senaryo ayrıştırma (kural tabanlı) ve veritabanına yazma
  engine/                   Yerel prosedürel render, sağlayıcı seçimi, kademeli iterasyon
  quality/                  Quality Gate ve FFmpeg anomali tespiti
  post/                     Ses, müzik, montaj, film birleştirme
  budget/                   Kill-Switch ve fiyatlandırma
  core/                     Stateless Re-render planlayıcısı
  live/                     ÜCRETLİ entegrasyonlar (fal.ai, ElevenLabs, LangGraph); DRY_RUN=1 iken kilitli
  api/                      FastAPI uç noktaları
  panel/                    Kontrol paneli (tek sayfa)
  worker/                   Celery görevleri
```

## Sorun giderme

- **Üretim çok yavaş:** `.env` içinde `LOCAL_RENDER_FPS=12` ve `GEN_CONCURRENCY=2` yap, `docker compose up -d` ile yeniden başlat.
- **Sahne "Yönetmen onayı gerekli" durumunda kaldı:** Sahte skorlar eşiği geçemedi. "Bu sahneyi baştan üret" ile yeni seed'lerle dene.
- **Videolar panelde açılmıyor:** MinIO'nun 9000 portu açık olmalı (`docker compose ps`).
- **Port meşgul:** `docker-compose.yml` içinde soldaki port numarasını değiştir.
- **Her şeyi sıfırla:** `docker compose down -v && docker compose up -d --build`

## Kademe 2'ye geçiş (ücretli, henüz yapma)

Kod hazır ama kilitli: `app/live/`. Geçerken `requirements-live.txt` kurulur, `.env` içinde anahtarlar girilir ve `DRY_RUN=0` yapılır. Önce sağlayıcı panellerinde aylık harcama limiti koy ve küçük bir sanal bütçeyle başla.
