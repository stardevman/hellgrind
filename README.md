# Hell Grind — Otonom AI Animasyon Fabrikası

Senaryodan film üreten otonom bir animasyon hattı. Bu aşamada hattın **tamamı yerel ve ücretsiz** bileşenlerle çalışır: hiçbir ücretli servis çağrılmaz, hiçbir kredi kartı gerekmez. Üretim, bir kontrol paneliyle izlenir ve yönetilir. Uzun vadeli hedef 90 dakikalık bir filmdir.

![Kontrol paneli](docs/panel.png)

## Neler yapar

1. **Senaryoyu okur.** Standart senaryo biçimini (sahne başlığı, konuşan karakter, aksiyon) kural tabanlı ayrıştırıcıyla çekimlere böler. Her çekim için kamera, duygu, süre ve aksiyon yoğunluğu çıkarır.
2. **Çekimleri üretir.** Her çekim için önce hızlı bir *test turu*, sonra dalgalar halinde *tam tur* klip üretir. Ücretsiz modda klipler yerel bir prosedürel motorla çizilir.
3. **Kalite kontrolünden geçirir.** Siyah ya da donmuş klipleri gerçek FFmpeg filtreleriyle bulur, eşiği geçemeyeni anında eler ve geriye en iyi birkaç klibi *rafine havuz* olarak bırakır.
4. **Sesi ve montajı yapar.** Türkçe konuşma (espeak-ng), duyguya göre fon müziği, diyalog altında müziğin kısılması, ses seviyesi dengeleme.
5. **Filmi birleştirir.** Hazır sahneleri sırayla tek videoda toplar ve tıklanabilir bir kesme listesi üretir.
6. **Parayı korur.** Her çağrı önce bütçeden rezervasyon alır. Limit aşılacaksa proje anında donar (Kill-Switch). Ücretsiz modda harcama, gerçek sağlayıcı fiyatlarıyla *sanal* olarak tutulur.

Bir şeyi değiştirdiğinizde yalnızca etkilenen aşamalar yeniden çalışır:

| Değişen girdi | Yeniden çalışanlar | Görüntü motoru |
|---|---|---|
| İstem, kamera, motor, süre ya da karakter sürümü | Görüntü, ses, montaj | çağrılır |
| Bir diyalog satırı | Ses ve montaj | çağrılmaz |
| Küratör rafine havuzdan başka bir klip seçer | Ses ve montaj | çağrılmaz |
| Yalnızca müzik ya da müzik seviyesi | Yalnızca montaj | çağrılmaz |
| Hiçbiri | Hiçbiri | çağrılmaz |

## Gereksinimler

- Docker ve Docker Compose
- Boş portlar: `8000` (panel ve API), `9000` ve `9001` (MinIO), `5555` (Flower), `5433` (PostgreSQL), `6379` (Redis)

FFmpeg, espeak-ng ve yazı tipleri Docker imajının içindedir; makinenize bir şey kurmanız gerekmez.

## Kurulum

```bash
cp .env.example .env          # varsayılanlar ücretsizdir; ücretli anahtarlar boş kalır
docker compose up -d --build  # tüm servisleri derler ve başlatır
curl localhost:8000/healthz   # {"ok":true,"dry_run":true}
```

Servisler: `postgres`, `redis`, `minio`, `api`, `worker`, `beat`, `flower`. Veritabanı şeması ilk açılışta kurulur; sonraki şema değişiklikleri API açılışında `backend/migrations/*.sql` dosyalarından, veritabanı silinmeden uygulanır.

| Adres | Ne |
|---|---|
| http://localhost:8000 | Kontrol paneli |
| http://localhost:8000/docs | API belgeleri |
| http://localhost:9001 | MinIO konsolu (kullanıcı `minio`, parola `minio12345`) |
| http://localhost:5555 | Flower (görev kuyruğu) |

## Kullanım

1. Paneli açın. Henüz proje yoksa karşınıza **Yeni proje** formu gelir: proje adı, sanal bütçe, üretim yoğunluğu ve örnek senaryo hazır dolu olarak.
   - *Önizleme*: en çok 4 test ve 8 tam klip (örnek film için yeterli, hızlı).
   - *Brifing*: 10 test ve en çok 50 tam klip.
2. **Senaryoyu işle ve üret**'e basın. Sahneler satırlar halinde belirir; her satırdaki *film şeridi* o sahnenin klipleridir:
   - yeşil çerçeveli ve numaralı: rafine havuzda
   - gri ve kırmızı çapraz çizgili: kalite kontrolünde elendi
   - soluk: havuza giremedi
   - amber ve nefes alan: üretiliyor
3. Bir sahneye ya da bir kareye tıklayın. **Denetçi** sonuç videosunu, rafine havuzu, çekim bilgilerini ve diyaloğu gösterir. Elenen klipler de (karantinadakiler dahil) önizlenir.
4. Denetçiden:
   - **Bunu kullan**: havuzdan başka bir klip seçer; yalnızca ses ve montaj yeniden çalışır.
   - **Müziği değiştir**: duygu ve seviye; yalnızca montaj yeniden çalışır.
   - **Bu sahneyi baştan üret**: görüntüyü yeni klip adaylarıyla yeniden üretir.
   Her eylemden sonra panel hangi aşamaların çalışacağını yazar.
5. **Tamamlanan N sahneyi birleştir** filmi üretir. Kesme şeridindeki bir sahneye tıklamak oynatıcıyı oraya götürür.
6. Bütçe rayındaki **Üretimi durdur** düğmesi Kill-Switch'tir: kuyruktaki işler iptal olur, yeni iş başlamaz. **Üretime devam et** kaldığı yerden sürdürür; tamamlanmış klipler yeniden üretilmez ve yeniden ücretlendirilmez.

API'yi doğrudan da kullanabilirsiniz:

```bash
curl -s localhost:8000/api/meta | jq .presets
curl -s -X POST localhost:8000/api/projects -H 'content-type: application/json' \
  -d '{"title":"Film","budget_limit_usd":60,"settings":{"probe_n":4,"full_n":8,"wave_size":4,"max_spend_per_10min":30}}'
curl -s localhost:8000/api/projects/<id>/overview | jq .scenes[0].status
```

### Senaryo biçimi

```
İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE          ← sahne başlığı (İÇ., DIŞ., İÇ/DIŞ., INT., EXT.)

Kızıl bir ışık kazanların arasından süzülüyor.   ← aksiyon: her paragraf yeni bir çekim

KEREM                                       ← konuşan karakter: büyük harfli, en çok 4 kelime
(tedirgin)                                  ← parantez: duygu ipucu
Burada kimse yok mu?
```

Karakter adındaki uzantılar (`KEREM (D.S.)`) atılır. Duygu, Türkçe kök sözlüğünden çıkarılır (korku, öfke, gerilim...). Aksiyon kelimeleri (koş, atla, patla, düş, kop, sarsıl...) çekimi geniş açı ve yüksek aksiyon yoğunluğuna çevirir. Yoğun aksiyon (≥ 0,7) Seedance'e, gerisi Wan'a yönlenir (ücretsiz modda yalnızca sanal fiyatı değiştirir).

## Testler

İki katman vardır:

```bash
make test    # tam paket: Docker içinde, PostgreSQL, Redis ve MinIO ile (~3 dk)
make smoke   # uçtan uca duman testi: gerçek HTTP ve worker ile (~30 sn)
```

- **`make test`**, veritabanı ve nesne deposu gerektiren testleri de çalıştırır. Geliştirme verinize dokunmaz: `hellgrind_test` adlı ayrı bir veritabanı ve `hellgrind-test` adlı ayrı bir kova kullanır. Depo kökündeki `db/` klasörünü bağlar ki testler şemayı kurabilsin.
- **CI** (`.github/workflows/ci.yml`) Ubuntu üzerinde FFmpeg ve espeak-ng ile `DRY_RUN=1 pytest -q` çalıştırır. CI'da veritabanı servisi yoktur; altyapı gerektiren testler kendiliğinden *atlanır*. Tam doğrulama `make test` iledir.
- **`make smoke`** şunları denetler: ücretli çağrılar engelli; örnek senaryo 4 sahneye ayrılıp hepsi `done` olur, her sahnede rafine havuz 1–3 klip, bilerek bozulan siyah ve donmuş klipler elenmiştir; yalnızca müzik değişince yalnızca montaj çalışır ve harcama değişmez; küratör seçimi yalnızca ses ve montajı çalıştırır; değişiklik yoksa plan boştur; film 4 sahneyle birleşir; 1 USD'lik projede sahne `frozen`, proje `paused` olur, harcama 1 USD'yi aşmaz ve yeni render isteği 423 döner. Başarıyla biterse **TÜM KONTROLLER GEÇTİ** yazar. Geliştirme veritabanına `duman-testi` başlıklı projeler ekler, hiçbir şeyi silmez.

## Bütçe ve Kill-Switch

- Her çağrı önce `reserve` (atomik, limit aşılacaksa reddeder ve projeyi dondurur), sonra `commit` ya da hata durumunda `release` yapar.
- `spent ≥ limit × soft_budget_ratio` olunca *yumuşak limit* bayrağı kalkar: yeni tam dalga başlatılmaz, sahneler eldeki adaylarla tamamlanır.
- Hız bekçisi 30 saniyede bir son 10 dakikanın harcamasına bakar; `max_spend_per_10min` aşılırsa projeyi dondurur. Panel, yeni projeyi bu sınır bütçenin yarısı olacak şekilde açar: örnek film saniyeler içinde yaklaşık 25-30 sanal USD harcar ve varsayılan 20 USD sınırı filmin ortasında projeyi dondururdu.
- Bayat rezervasyon toplayıcı 5 dakikada bir, 60 dakikadan eski ve eşi olmayan rezervasyonları iade eder.
- Fiyatlar `backend/pricing.yaml` dosyasındadır (fal.ai, Ekim 2026). Periyodik olarak güncelleyin.

## Ücretli mod kilitli

`DRY_RUN=1` varsayılandır. Bu modda anahtar tanımlı olsa bile hiçbir ücretli API çağrılmaz:

- Ücretli kod yalnızca `backend/app/live/` altında durur ve yalnızca fonksiyon içinde yüklenir.
- Her giriş noktası `guard(anahtar_adı)` çağırır; `DRY_RUN=1` ya da boş anahtar `PaidCallBlocked` fırlatır.
- Testler bunu otomatik denetler: ücretli paketler `app/live` dışında import edilemez, anahtarlar `guard` dışında okunamaz, ücretsiz modda hiçbir ücretli paket yüklenmez.
- Gizli anahtarlar yalnızca `.env` içinde durur; `.env` commit'lenmez.

Ücretli entegrasyonlar (fal.ai video, ElevenLabs ses, LLM ayrıştırıcı) bu aşamada **yazılmamıştır**; ücretli modda üretim, gerçek kalite skorlayıcı olmadığı için para harcanmadan önce durur. `DRY_RUN=0` ile hiçbir şeyi açık onay olmadan çalıştırmayın.

## Ayarlar

`.env` dosyasından okunur (örnek: `.env.example`). Başlıcaları:

| Ayar | Varsayılan | Anlamı |
|---|---|---|
| `DRY_RUN` | `1` | 1: hiçbir ücretli çağrı yapılmaz |
| `DRY_RUN_DEFECT_RATE` | `0.12` | Bilerek bozuk (siyah/donmuş) klip oranı: kalite kapısını sınamak için |
| `DRY_RUN_SCORE_MEAN` / `_SD` | `0.89` / `0.05` | Sahte CLIP skor dağılımı |
| `DRY_RUN_FAIL_RATE` | `0` | Geçici hata oranı (yeniden deneme testi için) |
| `GEN_CONCURRENCY` | `3` | Bir dalgada eşzamanlı üretim sayısı |
| `PURGE_MODE` | `quarantine` | `quarantine`: elenenler `quarantine/` önekine taşınır; `hard`: silinir |
| `TTS_ENGINE` | `espeak` | `espeak` ya da `tone` (yapay ton) |

Proje ayarları (proje oluştururken verilir): `clip_threshold` 0,85 · `excellent_threshold` 0,92 · `min_pass_rate` 0,3 · `probe_n` 10 · `full_n` 50 · `wave_size` 10 · `refined_k` 3 · `max_correction_rounds` 2 · `probe_resolution` 480p · `full_resolution` 720p · `soft_budget_ratio` 0,8 · `max_spend_per_10min` 20 · `loudness_lufs` −23.

## Depo yapısı

```
docker-compose.yml  Makefile  .env.example
db/init/001_core.sql           ilk şema (PostgreSQL ilk açılışta uygular)
backend/
  pricing.yaml  migrations/    fiyat tablosu, şema değişiklikleri
  app/
    config.py db.py models.py schemas.py storage.py migrate.py main.py
    budget/      killswitch (rezervasyon, dondurma, bekçiler), pricing
    core/        hashing, rerender (Stateless Re-render planlayıcısı), state
    hermes/      rules (ayrıştırıcı), ingest (veritabanına yazma)
    engine/      procedural (yerel render), providers, iteration (kademeli üretim)
    quality/     anomaly (FFmpeg), gate (skor, rafine havuz)
    post/        audio, mux, pipeline (LIPSYNC ve MUX aşamaları), timeline (film)
    api/         projects, scenes, budget, panel, plan, deps
    worker/      celery_app, tasks, dispatch
    live/        ücretli kilit (guard)
    panel/       index.html (tek sayfa, build adımı yok)
  scripts/smoke.py
  tests/
```

## Sorun giderme

- **Panel boş ya da "Sunucuya ulaşılamıyor" diyor:** `docker compose ps` ile servisleri, `docker compose logs api worker` ile günlükleri kontrol edin.
- **Üretim başlamıyor:** worker çalışıyor mu? Proje dondurulmuş olabilir (bütçe rayındaki uyarı bandı); *Üretime devam et*'e basın.
- **Sıfırdan başlamak:** `make reset` veritabanı ve MinIO dahil **her şeyi siler**; çalıştırmadan önce emin olun.
- **Yazı tipi yüklenmiyor:** panel Archivo'yu Google Fonts'tan alır; çevrimdışıyken sistem yazı tipine düşer, işlev etkilenmez.
