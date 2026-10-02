# Hell Grind — Otonom AI Animasyon Fabrikası

Senaryodan film üreten otonom bir animasyon hattı. Bu aşamada hattın **tamamı yerel ve ücretsiz** bileşenlerle çalışır: hiçbir ücretli servis çağrılmaz, hiçbir kredi kartı gerekmez. Üretim, bir kontrol paneliyle izlenir ve yönetilir. Uzun vadeli hedef 90 dakikalık bir filmdir.

![Kontrol paneli](docs/panel.png)

## Neler yapar

1. **Senaryoyu okur.** Standart senaryo biçimini (sahne başlığı, konuşan karakter, aksiyon) kural tabanlı ayrıştırıcıyla çekimlere böler. Her çekim için kamera, duygu, süre ve aksiyon yoğunluğu çıkarır.
2. **Eksik referansla üretmez.** Model eksik bilgiyi uydurur: çekimde geçen her karakter, mekân ve nesnenin onaylı ve kilitli bir referansı yoksa o çekim *engellenir* ve hiçbir şey harcanmaz. Referanslar *Varlıklar* sekmesinde yüklenir, kısa bir hareket testinden geçer ve yönetmen onayıyla kilitlenir.
3. **Tek bir stil dili kullanır.** Işık dili, hareket tarzı ve render kuralları projenin sürümlü *stil kılavuzundan* gelir ve her çekimin prompt'una aynı ifadeyle eklenir. Kılavuz değişince önce hangi çekimlerin yeniden üretileceği ve kaça mal olacağı gösterilir; yönetmen onaylamadan hiçbir şey üretilmez.
4. **Önce storyboard, sonra kaba kurgu.** Her çekimin storyboard karesi yönetmen onayından geçer (istenirse zorunlu). Taslak geçiş yalnızca test turuyla filmin kaba kurgusunu çıkarır; yalnızca zayıf bulunan çekimler tam turla iyileştirilir.
5. **Her çekim için yöntem seçtirir.** Çekim yapay zekâyla üretilebilir, bir previs (3D taslak) rehberinden çıkarılabilir ya da dışarıda bitirilip yüklenebilir. Üretilen klipler elle düzeltilip yeniden yüklenebilir; orijinal korunur.
6. **Çekimleri üretir.** Her çekim için önce hızlı bir *test turu*, sonra dalgalar halinde *tam tur* klip üretir. Ücretsiz modda klipler yerel bir prosedürel motorla çizilir.
7. **Kalite kontrolünden geçirir.** Siyah ya da donmuş klipleri gerçek FFmpeg filtreleriyle bulur, eşiği geçemeyeni anında eler ve geriye en iyi birkaç klibi *rafine havuz* olarak bırakır.
8. **Sesi ve montajı yapar.** Türkçe konuşma (espeak-ng), diyalog altında müziğin kısılması, ses seviyesi dengeleme.
9. **Filmi birleştirir.** Hazır sahneleri sekanslarına göre sırayla tek videoda toplar. Müzik sekans düzeyindedir ve çekimler arasında kesilmeden akar. Tıklanabilir bir kesme listesi üretir.
10. **Parayı korur.** Her çağrı önce bütçeden rezervasyon alır. Limit aşılacaksa proje anında donar (Kill-Switch). Ücretsiz modda harcama, gerçek sağlayıcı fiyatlarıyla *sanal* olarak tutulur.

Bir şeyi değiştirdiğinizde yalnızca etkilenen aşamalar yeniden çalışır:

| Değişen girdi | Yeniden çalışanlar | Görüntü motoru |
|---|---|---|
| İstem, kamera, motor, süre, karakter sürümü, bir varlığın referansı ya da etkin stil kılavuzu | Storyboard karesi, görüntü, ses, montaj | çağrılır |
| Bir diyalog satırı | Ses ve montaj | çağrılmaz |
| Küratör rafine havuzdan başka bir klip seçer | Ses ve montaj | çağrılmaz |
| Rafine #1 elle düzeltilir (revizyon yüklenir) ya da orijinaline dönülür | Ses ve montaj | çağrılmaz |
| Dış çekim yüklenir | Ses ve montaj | çağrılmaz, harcama yok |
| Bir çekime özel müzik ya da seviyesi | Yalnızca montaj | çağrılmaz |
| Sekans ya da film müziği, bestecinin stem'i | Yalnızca film birleştirme | çağrılmaz |
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
2. **Senaryoyu işle ve üret**'e basın. Sahneler satırlar halinde belirir, ama ilk seferde hepsi **Engellendi** durumundadır: senaryodaki karakterlerin (Kerem, Ayşe) ve mekânların (Terk Edilmiş Dökümhane, Çöken Köprü) henüz onaylı referansı yoktur. Satırdaki *Eksik referans* bağlantıları sizi ilgili varlığa götürür.
3. **Varlıklar** sekmesinde her varlık için:
   1. Referans yükleyin (karakter için 360° dönüş videosu, ifade sayfası ya da ses örneği; mekân ve nesne için durağan görsel). Elinizde dosya yoksa **Yerel taslak üret** prosedürel motordan bir taslak referans çıkarır.
   2. **Durağanı onayla**.
   3. **Hareket testi üret**: kısa, düşük çözünürlüklü bir klip. Durağanda iyi görünen bir varlık hareket halinde bozulabilir; testi izleyin. Kusurlu (siyah ya da donmuş) test onaylanamaz, **Hareket testini yenile** ile yenisini üretin.
   4. **Hareket testini onayla**, sonra **Kilitle**. Hareket testi onaylanmadan varlık kilitlenemez.
   İsterseniz önce **Stil kılavuzu** sekmesinde **Örnekle doldur** (altın saat ışığı, çizgi film hareketi, suluboya mekân + temiz 3D nesneler) ile ilk sürümü kaydedip etkinleştirin; hareket testleri de bu stille üretilir.
4. Hepsi kilitlenince **Engellenen çekimleri yeniden dene**'ye basın. Üretim başlar; her satırdaki *film şeridi* o sahnenin klipleridir:
   - yeşil çerçeveli ve numaralı: rafine havuzda
   - gri ve kırmızı çapraz çizgili: kalite kontrolünde elendi
   - soluk: havuza giremedi
   - amber ve nefes alan: üretiliyor
5. Bir sahneye ya da bir kareye tıklayın. **Denetçi** sonuç videosunu, rafine havuzu, çekim bilgilerini ve diyaloğu gösterir. Elenen klipler de (karantinadakiler dahil) önizlenir.
6. Denetçiden:
   - **Bunu kullan**: havuzdan başka bir klip seçer; yalnızca ses ve montaj yeniden çalışır.
   - **Müziği değiştir**: duygu ve seviye. *Sekansın tamamı* (varsayılan) sekans müziğini değiştirir: hiçbir çekim yeniden üretilmez, yalnızca film yeniden birleştirilir. *Yalnızca bu çekim* çekime özel bir müzik koyar: yalnızca o çekimin montajı yeniden çalışır.
   - **Sekans**: bütçe tavanı, sekansın film kanalları ve **Stem yükle** (bestecinin zamanlanmış müzik dosyası; başlangıcı sekansın başına göredir).
   - **Bu sahneyi baştan üret**: görüntüyü yeni klip adaylarıyla yeniden üretir.
   - **Nesne bağla**: çekime bir nesne ekler (ör. "Taksi"); nesne taslak olarak oluşur ve kilitlenene dek çekimi engeller.
   Her eylemden sonra panel hangi aşamaların çalışacağını yazar.
7. **Tamamlanan N sahneyi birleştir** filmi üretir. Kesme şeridindeki bir sahneye tıklamak oynatıcıyı oraya götürür.
8. Bütçe rayındaki **Üretimi durdur** düğmesi Kill-Switch'tir: kuyruktaki işler iptal olur, yeni iş başlamaz. **Üretime devam et** kaldığı yerden sürdürür; tamamlanmış klipler yeniden üretilmez ve yeniden ücretlendirilmez.

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
Kerem bir taksiye biner. [NESNE: Taksi]          ← nesne: yalnızca bu çekime bağlanır

KEREM                                       ← konuşan karakter: büyük harfli, en çok 4 kelime
(tedirgin)                                  ← parantez: duygu ipucu
Burada kimse yok mu?
```

Mekân sahne başlığından çıkar, gün zamanı atılır: `İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE` → *Terk Edilmiş Dökümhane*. `[NESNE: Taksi, Bavul]` işareti bir paragrafın içindeyse o çekime, tek başına bir satırdaysa sahnenin bütün çekimlerine bağlanır; işaret metinden ve replikten silinir. Karakter, mekân ve nesneler ilk görüldüklerinde *taslak* varlık olarak oluşur.

Film sekanslara bölünür: `# SEKANS: Havalimanı` satırı yeni bir sekans başlatır ve sonraki sahne başlıkları ona aittir (sekans satırını sahne başlığından önce yazın). Hiç sekans satırı yoksa bütün sahneler tek sekanstadır (*Sekans 1*). Örnek senaryo iki sekanslıdır: *Dökümhane* ve *Köprü*.

Karakter adındaki uzantılar (`KEREM (D.S.)`) atılır. Duygu, Türkçe kök sözlüğünden çıkarılır (korku, öfke, gerilim...). Aksiyon kelimeleri (koş, atla, patla, düş, kop, sarsıl...) çekimi geniş açı ve yüksek aksiyon yoğunluğuna çevirir. Yoğun aksiyon (≥ 0,7) Seedance'e, gerisi Wan'a yönlenir (ücretsiz modda yalnızca sanal fiyatı değiştirir).

## Testler

İki katman vardır:

```bash
make test    # tam paket: Docker içinde, PostgreSQL, Redis ve MinIO ile (~4 dk)
make smoke   # uçtan uca duman testi: gerçek HTTP ve worker ile (~2 dk)
```

- **`make test`**, veritabanı ve nesne deposu gerektiren testleri de çalıştırır. Geliştirme verinize dokunmaz: `hellgrind_test` adlı ayrı bir veritabanı ve `hellgrind-test` adlı ayrı bir kova kullanır. Depo kökündeki `db/` klasörünü bağlar ki testler şemayı kurabilsin.
- **CI** (`.github/workflows/ci.yml`) Ubuntu üzerinde FFmpeg ve espeak-ng ile `DRY_RUN=1 pytest -q` çalıştırır. CI'da veritabanı servisi yoktur; altyapı gerektiren testler kendiliğinden *atlanır*. Tam doğrulama `make test` iledir.
- **`make smoke`** şunları denetler: ücretli çağrılar engelli; ana projede kare onayı zorunludur: onaysız çekimlerde hiçbir varyasyon üretilmez, storyboard PDF'i dört çekimi sırayla içerir, taslak geçiş yalnızca test turu çalıştırır ve kaba kurguyu kendiliğinden birleştirir, iyileştirme geçişi yalnızca işaretli çekimi tam tura alır; film altın saat stil kılavuzuyla üretilir ve iki farklı çekimin prompt'u aynı önekle başlar; stil değişince önizleme dört çekimi ve sanal maliyeti listeler, onaydan önce hiçbir şey üretilmez, etkinleştirince çekimler yeni önekle yeniden üretilir; yeni projede örnek senaryonun dört çekimi de engellenir ve hiçbir şey harcanmaz; taslak varlık kilitlenemez; gerçek bir dönüş videosu yüklenince 8 kare örneklenir; varlıklar hareket testinden geçip kilitlenince çekimler üretilir, panelden bağlanan taslak bir nesneyi içeren çekim kilitlenene dek engelli kalır ve hiç harcama yapmaz; örnek senaryo 4 sahneye ayrılıp hepsi `done` olur, her sahnede rafine havuz 1–3 klip, bilerek bozulan siyah ve donmuş klipler elenmiştir; yalnızca müzik değişince yalnızca montaj çalışır ve harcama değişmez; küratör seçimi yalnızca ses ve montajı çalıştırır; değişiklik yoksa plan boştur; film 4 sahneyle birleşir ve sekans müziği kesmelerde sürer; dış çekim üretim harcaması olmadan biter ve rafine #1'in elle düzeltmesi yalnızca ses ve montajı çalıştırır; sekans müziği değişince yalnızca film yeniden birleştirilir; sekans tavanı dolunca yalnızca o sekans durur, diğeri biter; 1 USD'lik projede sahne `frozen`, proje `paused` olur, harcama 1 USD'yi aşmaz ve yeni render isteği 423 döner. Başarıyla biterse **TÜM KONTROLLER GEÇTİ** yazar. Geliştirme veritabanına `duman-testi` başlıklı projeler ekler, hiçbir şeyi silmez.

## Varlık kütüphanesi ve referans eksiksizliği

Referans verilmeyen bir taksiye model rastgele yolcular ekler. Bu yüzden:

- Her karakter, mekân ve nesne bir **varlıktır**. Durum akışı: `draft` (taslak) → `still_approved` (durağan onaylı) → `motion_tested` (hareket testi onaylı) → `locked` (kilitli).
- **Üretim öncesi kontrol** (`hermes/lint.py`): görüntü üretimi planlanan bir çekimde gereken durumun altında bir varlık varsa çekim `blocked` olur, `last_error` alanına neyin eksik olduğu Türkçe yazılır ve hiçbir rezervasyon yapılmaz. Kontrol hem kuyruğa vermeden önce hem de worker'da üretim başlamadan önce yapılır.
- Prompt'a her zaman "yalnızca listelenen karakterler" kısıtı eklenir; negatif prompt'a kalabalık ve fazladan kişiler yazılır.
- **Karakter referans paketi:** 360° dönüş videosu, ifade sayfası, ses örneği ve `voice_id`. Dönüş videosundan eşit aralıkla 8 kare örneklenir; ilki yüz referansıdır. Vektörler ücretsiz modda karelerin HSV histogramlarından yerel olarak hesaplanır (gerçek yüz vektörleri ücretli aşamadadır). Ses kimliği karakterde kalır.
- Görsel bir referans değişince varlığın **sürümü** artar ve varlık taslağa döner; sürüm görsel özete girdiği için o varlığı kullanan çekimler yeniden üretilir. Onay ve kilit durumu özete girmez: kilitlemek tek başına yeniden üretim tetiklemez. Aynı dosyayı yeniden yüklemek hiçbir şeyi değiştirmez. Kilitli varlık değiştirilemez; önce kilidi açılır.
- **Hareket testi** her üretim gibi bütçe rezervasyonundan geçer (ücretsiz modda sanal, Wan 480p fiyatıyla) ve gerçek anomali tespitinden geçer. Test varlığın sürümüne bağlıdır: referans ya da tarif değişince eski test onaylanamaz.
- API: `GET/POST /api/projects/{id}/assets`, `GET/PATCH /api/assets/{id}`, `POST /api/assets/{id}/references?kind=still|turnaround|expressions|voice_sample&filename=...` (dosya istek gövdesinde), `POST /api/assets/{id}/references/placeholder`, `POST /api/assets/{id}/approve-still`, `/motion-test`, `/approve-motion`, `/lock`, `/unlock`, `POST /api/scenes/{id}/assets` ve `DELETE /api/scenes/{id}/assets/{asset_id}`.

## Sekanslar ve film düzeyinde ses

- Her sahne bir sekansa aittir (`SEQ01`, `SEQ02`...). Panelde sahneler sekans başlıkları altında gruplanır; başlıkta çekim sayısı, harcama ve tavan görünür.
- **Müzik sekans düzeyindedir.** Senaryo yüklenince her sekans, çekimlerinin baskın duygusuna (sürelerine göre) uygun prosedürel bir müzik kanalı alır (−8 dB, yumuşak giriş ve çıkış). Çekimlere müzik eklenmez; çekime özel fon (ör. tek çekimlik efekt) isteğe bağlıdır. Faz 11'den önce oluşturulmuş projelerde çekim müzikleri yerinde kalır ve sekans müziği eklenmez (film çift müzik çalmasın).
- **Film birleştirme:** önce sahne klipleri kendi sesleriyle (diyalog ve varsa çekime özel fon) birleştirilir; sonra zaman çizelgesi kanalları sekans aralıklarına yerleştirilir (sekansın sonunu aşmaz), diyalog müziği sidechain ile bastırır ve en sonda loudnorm uygulanır. Kanalların içerik özetleri ve zamanlamaları film anahtarına girer.
- **Besteci desteği:** `POST /api/sequences/{id}/tracks?filename=tema.wav&start_s=4&gain_db=-6&fade_in_s=1` (dosya istek gövdesinde) zamanlanmış bir stem ekler; bir sekansta birden çok stem olabilir. JSON gövdeyle (`{"mood": "joy", "gain_db": -10}`) prosedürel müzik değişir; yüklenen stem'lere dokunulmaz. Film düzeyinde kanal için `POST /api/projects/{id}/tracks`. Kanallar `PATCH`/`DELETE /api/timeline-tracks/{id}` ile düzenlenir ya da kaldırılır. Her değişiklikte film kendiliğinden yeniden birleştirilir; hiçbir çekim yeniden üretilmez.
- **Sekans bütçe tavanı:** `PATCH /api/sequences/{id}` ile `budget_cap_usd`. Rezervasyon, proje limitinin yanında sekans tavanını da aynı işlemde denetler. Tavan dolunca yalnızca o sekans durur (`capped`), sahneleri `frozen` olur ve olay yazılır; proje ve diğer sekanslar üretmeye devam eder. Tavan yükseltilince ya da kaldırılınca sekans yeniden açılır. Projeyi *Üretime devam et* ile sürdürmek tavandaki sekansı açmaz.

## Storyboard, taslak geçiş ve kaba kurgu

Revizyonu en ucuz aşamada yapmak maliyeti en çok düşüren adımdır. Bu yüzden:

- **STORYBOARD aşaması** görüntü üretiminden önce gelir: her çekim için bir kare çizilir. Ücretsiz modda kare, prosedürel motorun çekim ortasındaki karesidir ve üzerine çekim kodu, kamera, süre ve replik yazılır; bedeli tek bir Flux karesinin fiyatıyla sanal olarak bütçeden düşer (ücretli Flux karesi Faz 18'de). Kare yalnızca görüntü yeniden üretilecekse ve çekimin görsel girdileri değiştiyse yeniden çizilir; bitmiş çekimler için kare istenmez.
- **Kare onayı:** *Storyboard* sekmesinde her kare için **Onayla** ya da not bırakıp **Değişiklik iste**. Görsel girdileri değişen çekimin karesi yeniden çizilir ve onay yeniden istenir (stil değişikliği dahil). **Görüntü üretimi için kare onayı zorunlu** (`require_board_approval`, varsayılan kapalı) açıkken onaylanmamış çekimde hiçbir varyasyon üretilmez: üretim isteği yalnızca kareyi çizer ve çekimi *Kare onay bekliyor* durumunda bırakır; kontrol worker'da da yapılır. **Kareleri hazırla** görüntü üretmeden yalnızca kareleri çizer (varlık kontrolü uygulanmaz: storyboard varlıklar kilitlenmeden önce de hazırlanabilir).
- **Storyboard PDF'i:** `GET /api/projects/{id}/storyboard.pdf` (panelde **PDF indir**). Sayfa başına 6 kare; her sekans yeni sayfada başlar; çekim kodu, kamera, süre, diyalog, onay durumu ve not yazılır.
- **Taslak geçiş** (`POST /api/projects/{id}/draft-pass`, panelde **Taslak geçişi başlat**): görüntüsü üretilecek her çekim (onay zorunluysa yalnızca onaylılar) yalnızca test turuyla üretilir ve test turunda geçen ilk varyasyon kullanılır; tam tur hiç çalışmaz. Son taslak çekim bitince **kaba kurgu** kendiliğinden birleştirilir.
- **İyileştirme geçişi:** kaba kurguda zayıf bulunan çekimler sahne satırındaki **İyileştir** ile işaretlenir; `POST /api/projects/{id}/upgrade-pass` yalnızca işaretli taslak çekimlerde tam turu çalıştırır. Test turu klipleri aynı seed'lerle yeniden kullanılır: yalnızca tam tur dalgaları ücretlendirilir. Taslak klibi kaba kurgu için saklanır.
- Film bölümünde **Kaba kurgu** ve **Final** ayrı filmlerdir (`POST /api/projects/{id}/assemble?kind=draft`). Kaba kurgu taslak klipleri, final ise her çekimin şimdiki montajını kullanır.
- Proje ayarları sonradan `PATCH /api/projects/{id}` ile değiştirilebilir (ör. `{"settings": {"require_board_approval": true}}`).

## Üretim yöntemi, dış çekimler ve elle düzeltme

Karmaşık ya da çok özel hareketleri tarif ederek üretmek zordur. Profesyonel ekipler bu çekimleri Blender'da elle canlandırır, üretilen görselleri de elle düzeltir. Her çekimin bir **yöntemi** vardır (denetçide *Yöntem*):

- **Yapay zekâ üretimi** (varsayılan): kademeli üretim.
- **Previs rehberi:** `POST /api/scenes/{id}/guide?filename=previs.mp4` (dosya istek gövdesinde) 3D taslak videoyu yükler. Görüntü aşaması rehberden çıkar: ücretsiz modda rehber, etkin stil kılavuzunun ışık diline göre renklendirilip doğrudan kullanılır (bedeli bir video-to-video geçişinin sanal fiyatı); ücretli modda video-to-video stil aktarımı Faz 18'dedir ve para harcanmadan durur. Rehber dosyanın özeti görsel özete girer; varlık ve kare onayı kapıları uygulanır.
- **Dış çekim:** `POST /api/scenes/{id}/external?filename=cekim.mp4` dışarıda bitirilmiş videoyu yükler. Görüntü üretilmez ve storyboard çizilmez; klip anomali kontrolünden geçer (siyah ya da donmuş klip reddedilir), rafine #1 olur, sonra ses ve montajdan geçer. Dosyanın içerik özeti görsel özetin yerine geçer. Hiçbir üretim harcaması yapılmaz.
- Yöntem `POST /api/scenes/{id}/method` ile değişir. Dış çekime geçerken önceki üretilmiş klipler silinmez; üretime dönülürse yeniden kullanılır.
- **Yönlendirici önerisi:** aksiyon yoğunluğu çok yüksek (≥ 0,8) ve kamera hareketi özel (el kamerası, takip, yörünge, vinç) olan çekimde panel previs rehberini önerir; karar yönetmenindir.

**Elle düzeltme:** `POST /api/variations/{id}/revisions?kind=video|first_frame&filename=...&note=...` düzenlenmiş videoyu ya da ilk kareyi yükler. Her revizyon yeni bir sürümdür ve orijinal varyasyon olduğu gibi kalır; aynı dosyayı yeniden yüklemek yeni sürüm açmaz, kusurlu revizyon reddedilir. İlk kare düzeltmesi ücretsiz modda klibin başında kısa süre durup orijinale erir (ücretli modda düzenlenmiş kareden yeniden üretim Faz 18'dedir). Düzeltilen klip rafine #1 ise dudak senkronu anahtarı revizyon kimliğini içerir ve yalnızca dudak senkronu ile montaj yeniden çalışır. Geçmiş `GET /api/variations/{id}/revisions` ile görülür; `POST /api/variations/{id}/revisions/active` başka bir sürüme ya da orijinale döner.

## Stil kılavuzu

Çekimlerin aynı filme ait görünmesini en çok tutarlı ışık dili sağlar. Profesyonel ekipler bir formül bulup (ör. suluboya mekân + altın saat ışığı) her çekimde aynı ifadeyle tekrarlar. Bu yüzden:

- Stil kılavuzu proje başına **sürümlüdür**: ışık öneki, hareket tarzı, render kuralları (varlık türü → `clean_3d`, `painterly`, `stylized_2d` ya da serbest metin), negatifler, palet ve notlar. Her düzenleme yeni bir sürümdür; aynı anda tek sürüm etkindir ve eski bir sürüm yeniden etkinleştirilebilir.
- **Prompt bestecisi** (`style/composer.py`) sağlayıcılara giden prompt'un tek kaynağıdır ve her zaman aynı sırayı izler: ışık öneki (ve palet), hareket tarzı, çekim tarifi ve kamera, her varlığın kendi render kuralıyla tarifi, "yalnızca listelenen karakterler" kısıtı, negatifler. Aynı sürümdeki her çekimin prompt'u aynı önekle başlar. Hareket testleri de besteciden geçer.
- Hareket eden şeyler temiz 3D, çevre boyalıdır (varsayılan kurallar): yoğun boyalı doku hareket halinde bozulur. Bir varlığın `render_style` alanı boşsa kılavuzun tür kuralı geçerlidir; doluysa yalnızca o varlık için istisnadır.
- Stil çekim bazında yazılamaz: bir sahnenin istemine `lighting`, `style`, `motion_style` gibi anahtarlar eklemek reddedilir.
- **Etkin sürüm görsel özete girer.** Bu yüzden değişiklik iki adımdır: `POST /api/projects/{id}/style/preview` hangi üretilmiş çekimlerin yeniden üretileceğini ve tahmini sanal maliyeti (beklenen: bir test turu + bir tam dalga; en kötü: tüm düzeltme turları + `full_n`) döner, hiçbir şey yazmaz. Yönetmen onaylarsa `POST /api/projects/{id}/style/activate` (`render: true` ile etkilenen çekimler hemen kuyruğa girer). Sürüm kaydetmek (`POST /api/projects/{id}/style`) hiçbir çekimi etkilemez.
- Ücretsiz modda bile stilin etkisi görünür: prosedürel motor ışık önekindeki anahtar kelimeleri yorumlar. `golden-hour` (ya da "altın saat") sıcak ve alçak bir güneş, uzun gölgeler ve ufka yakın sıcak parıltı verir; `night`/`moonlight` ay ışığı, `overcast` yumuşak ve soluk ışık. Tanınmayan bir önek render'ı hiç değiştirmez.

## Bütçe ve Kill-Switch

- Her çağrı önce `reserve` (atomik, limit aşılacaksa reddeder ve projeyi dondurur), sonra `commit` ya da hata durumunda `release` yapar.
- `spent ≥ limit × soft_budget_ratio` olunca *yumuşak limit* bayrağı kalkar: yeni tam dalga başlatılmaz, sahneler eldeki adaylarla tamamlanır.
- Hız bekçisi 30 saniyede bir son 10 dakikanın harcamasına bakar; `max_spend_per_10min` aşılırsa projeyi dondurur. Panel, yeni projeyi bu sınır bütçenin yarısı olacak şekilde açar: örnek film saniyeler içinde yaklaşık 25-30 sanal USD harcar ve varsayılan 20 USD sınırı filmin ortasında projeyi dondururdu.
- Bayat rezervasyon toplayıcı 5 dakikada bir, 60 dakikadan eski ve eşi olmayan rezervasyonları iade eder.
- Fiyatlar `backend/pricing.yaml` dosyasındadır (fal.ai, Ekim 2026). Periyodik olarak güncelleyin.

## Ücretli mod kilitli

`DRY_RUN=1` varsayılandır. Bu modda anahtar tanımlı olsa bile hiçbir ücretli API çağrılmaz:

- Ücretli kod yalnızca `backend/app/live/` altında durur ve yalnızca fonksiyon içinde yüklenir.
- Her giriş noktası `guard(anahtar_adı)` çağırır; `DRY_RUN=1` ya da boş anahtar `PaidCallBlocked` fırlatır. Kilit, bütçe rezervasyonundan **önce** çalışır; engellenen çağrı deftere iz bırakmaz.
- Testler bunu otomatik denetler: ücretli paketler `app/live` dışında import edilemez, anahtar değerleri `guard` dışında okunamaz, ücretsiz modda hiçbir ücretli paket yüklenmez, her giriş noktası tuzaklı bir ortamda hiçbir şeye dokunmadan engellenir.
- Gizli anahtarlar yalnızca `.env` içinde durur; `.env` commit'lenmez.

### Ücretli entegrasyonlar: yazıldı, hiç çalıştırılmadı

| Modül | Ne yapar |
|---|---|
| `live/fal_video.py` | Flux LoRA ile ilk kare (`fal-ai/flux-lora`), sonra Wan 2.2 (`fal-ai/wan/v2.2-a14b/image-to-video`) ya da Seedance (`bytedance/seedance-2.0/fast/image-to-video`); dudak senkronu `fal-ai/sync-lipsync/v2/pro`. 429, 5xx ve zaman aşımı yeniden denenir. |
| `live/elevenlabs.py` | `eleven_multilingual_v2`; duygu `stability` ve `style` değerlerine eşlenir. |
| `live/hermes_llm.py` | LangGraph akışı: `Send` ile paralel çıkarma, doğrulama, en çok 2 tur onarım. Çıktı şeması kural tabanlı ayrıştırıcıyla aynı; replikler eksiltilemez ya da uydurulamaz. |

Uç nokta kimlikleri ve parametreler fal.ai ve ElevenLabs belgelerinden doğrulandı (2 Ekim 2026), paket imzaları gerçek paketlerden denetlendi. Testler sahte `fal_client`, sahte `httpx` ve sahte LLM kullanır; genel internete bağlantı testlerde yasaktır. Hiçbir ücretli çağrı hiçbir zaman yapılmadı. Her ücretli çağrı bütçe rezervasyonundan geçer (LLM çağrıları belirteç tahminiyle, sesler karakter sayısıyla, video ve dudak senkronu fiyat tablosuyla).

Bilinmesi gerekenler:

- fal yanıtı fatura tutarı döndürmez; harcama `pricing.yaml` tahminidir. `tts_per_1k_chars` ve `llm_per_million_tokens` kalemleri doğrulanmış değildir: ücretli moda geçmeden önce kendi planınıza göre güncelleyin.
- Seedance en az 4 sn üretir ve tohum kabul etmez; 3 sn'lik bir çekim 4 sn faturalanır. Wan 17–161 kare (16 fps) üretir.
- **Ücretli modda üretim henüz başlatılamaz**: gerçek kalite skorlayıcı (OpenCLIP stil, InsightFace yüz) yazılmadı ve sahte skor ücretli klipleri sessizce onaylayabileceğinden, görsel üretim para harcanmadan önce durur. Ücretli mod için sırada: gerçek skorlayıcı, düşük skorda istemi revize eden düzeltme grafiği, Pinecone ile karakter vektörleri ve iki katmanlı önbellek.
- Ücretli entegrasyonları denemek için ayrıca `pip install -r backend/requirements-live.txt` gerekir. Bunu açık onay olmadan yapmayın; `DRY_RUN=0` ile hiçbir şeyi açık onay olmadan çalıştırmayın.

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
| `TTS_ENGINE` | `espeak` | `espeak`, `tone` (yapay ton) ya da `elevenlabs` (ücretli; ücretsiz modda espeak kullanılır) |

Proje ayarları (proje oluştururken verilir): `clip_threshold` 0,85 · `excellent_threshold` 0,92 · `min_pass_rate` 0,3 · `probe_n` 10 · `full_n` 50 · `wave_size` 10 · `refined_k` 3 · `max_correction_rounds` 2 · `probe_resolution` 480p · `full_resolution` 720p · `soft_budget_ratio` 0,8 · `max_spend_per_10min` 20 · `loudness_lufs` −23 · `lint_strictness` block (`off`: kontrol yok, `warn`: olay yazılır ama üretilir, `block`: engellenir) · `asset_min_status` locked (ya da `still_approved`, `motion_tested`).

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
    hermes/      rules (ayrıştırıcı), ingest (veritabanına yazma), lint (üretim öncesi referans kontrolü)
    engine/      procedural (yerel render), providers, iteration (kademeli üretim),
                 references (kare örnekleme, vektör, yerel taslak), motion_test (hareket testi),
                 storyboard (kare, onay kapısı, PDF), methods (previs rehberi, dış çekim, elle düzeltme)
    style/       bible (sürümler, etkin sürüm, etki önizlemesi), composer (prompt bestecisi)
    quality/     anomaly (FFmpeg), gate (skor, rafine havuz)
    post/        audio, mux, pipeline (LIPSYNC ve MUX aşamaları), timeline (film ve sekans müziği)
    api/         projects, scenes, methods, sequences, storyboard, assets, style, budget, panel, plan, deps, uploads
    worker/      celery_app, tasks, dispatch
    live/        ücretli kilit (guard), fal_video, elevenlabs, hermes_llm
    panel/       index.html (tek sayfa, build adımı yok)
  requirements-live.txt        yalnızca ücretli entegrasyonlar için ek bağımlılıklar
  scripts/smoke.py
  tests/
```

## Sorun giderme

- **Panel boş ya da "Sunucuya ulaşılamıyor" diyor:** `docker compose ps` ile servisleri, `docker compose logs api worker` ile günlükleri kontrol edin.
- **Sahneler "Engellendi" diyor:** çekimdeki bir varlık henüz kilitli değil. Satırdaki eksik referans bağlantısına tıklayıp varlığı onay akışından geçirin, sonra *Engellenen çekimleri yeniden dene*'ye basın. Referans kontrolünü bir proje için kapatmak isterseniz proje ayarlarında `lint_strictness: "off"` verin.
- **Üretim başlamıyor:** worker çalışıyor mu? Proje dondurulmuş olabilir (bütçe rayındaki uyarı bandı); *Üretime devam et*'e basın.
- **Sıfırdan başlamak:** `make reset` veritabanı ve MinIO dahil **her şeyi siler**; çalıştırmadan önce emin olun.
- **Yazı tipi yüklenmiyor:** panel Archivo'yu Google Fonts'tan alır; çevrimdışıyken sistem yazı tipine düşer, işlev etkilenmez.
