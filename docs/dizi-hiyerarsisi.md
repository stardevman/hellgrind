# Dizi hiyerarşisi: tasarım notu (Paket 6, onay bekliyor)

**Hedef yapı:** Dizi › Sezon › Bölüm › Sekans › Çekim.
Bu not yalnızca tasarımdır; onay gelmeden kod yazılmayacak.

## Temel karar: bölüm = bugünkü proje

Bugün bütçe, Kill-Switch, sürümler (v001…), müşteri kapıları, teslim paketi ve kürasyon kaydı **proje** başına çalışıyor. Bölümü mevcut `projects` satırına eşlemek, üretim hattına dokunmadan iki yeni üst katman eklemek demek: dizi ve sezon.

- Sezonu olmayan proje (`season_id` boş) bugünkü gibi bağımsız bir film olarak kalır. Geriye dönük taşıma gerekmez.
- Sekans ve çekim aynen kalır.

## Panel

**Üst banttaki proje seçici bir kırıntı yoluna dönüşür:** `Dizi › Sezon 1 › Bölüm 3`. Panelde bant artık altta; menüler yine yukarı açılır.

- Her kırıntı kendi menüsünü açar:
  - dizi listesi ve "Yeni dizi";
  - sezonlar;
  - bölümler: numara, ad, durum ve teslim rozeti.
- URL'de yalnızca `?p=<bölüm>` kalır; dizi ve sezon bölümden türetilir. Paylaşılan bağlantılar bozulmaz.
- Bağımsız film tek parça görünür: `Film adı`.

**Varlıklar sayfası:** tür ve durum süzgeçlerinin yanına kapsam süzgeci gelir: **Dizi kütüphanesi / Bu bölüm**.

- Dizi kütüphanesindeki varlık bütün bölümlerde kullanılabilir.
- Bölüme özel varlık yalnızca o bölümde görünür.
- Bir dizi varlığı bölüme "bağlanır", kopyalanmaz.

**"Sürüm sabit" rozeti:** Kabul kapısı onaylanan bölüm teslim edilmiş sayılır. Bölüm, Kabul'deki film sürümüne ve o anki varlık sürümlerine sabitlenir.

- Panelde bölümün yanında ve kırıntıda `Sürüm sabit · v004` rozeti görünür.
- Sabit bölümde üretim, stil etkinleştirme ve varlık sürümü güncellemesi uygulanmaz.
- Kilidi yalnızca yönetici açabilir, ayrı bir onayla; bu da kürasyon kaydına düşer.

## Asıl risk: paylaşılan varlığın sürümü

Görsel özet (`visual_fingerprint`) bağlı varlıkların sürümünü içeriyor. Bir dizi karakterinin referansı yenilenince sürümü artar. Önlem alınmazsa **bütün bölümlerin** çekimleri "değişmiş" görünür ve yeniden üretilmek ister.

**Öneri:** bölüm ile varlık arasındaki bağ, sürümü sabitleyebilir (`episode_assets.pinned_version`).

- Teslim edilmiş bölüm her zaman sabit sürümü kullanır.
- Üretimdeki bölüm "dizi kütüphanesindeki en son sürümü kullan" ayarıyla açılır. Yeni sürüme geçiş, bugünkü stil önizlemesi gibi önce etkisini gösterir: hangi çekimler yeniden üretilir, tahmini maliyet ne.

## Tablolar ve migration sırası

**035_series.sql**
- `series (id, title, settings jsonb, budget_cap_usd, created_at)`
- `seasons (id, series_id, number, title, budget_cap_usd)`, `UNIQUE (series_id, number)`
- `projects.season_id` (boş olabilir) ve `projects.episode_no`, `UNIQUE (season_id, episode_no)`

**036_series_assets.sql**
- `assets.series_id` eklenir; `assets.project_id` boş olabilir hâle gelir. CHECK: ikisinden tam biri dolu.
- `characters.series_id` aynı biçimde; ses kimliği (`voice_id`) dizi düzeyinde yaşar.
- `episode_assets (project_id, asset_id, pinned_version NULL, added_at)`, PK `(project_id, asset_id)`.
- Okuma kuralı: bölümdeki varlık kümesi = bölüme özel varlıklar ∪ bölüme bağlanmış dizi varlıkları. `lint`, `load_assets` ve görsel özet bu kümeyi kullanır; görsel özet `pinned_version` varsa onu alır.

**037_episode_lock.sql**
- `projects.locked_at`, `projects.locked_version_id` (→ `film_versions`).
- Kabul kapısı onaylanınca dolar. `plan_and_dispatch` ve stil etkinleştirme kilitli bölümde 423 döner; dondurmadaki gibi Türkçe gerekçeyle.

**038_series_budget.sql** (isteğe bağlı): `reserve`, sekans tavanının yanında sezon ve dizi tavanını da denetler. Kural aynıdır: tavan dolunca yalnızca o kapsamın işleri durur.

## Kapsam dışı bırakılanlar

- **Stil kılavuzu** ilk adımda bölüm başına kalır. Dizi düzeyi stil, "dizi stilini bu bölüme kopyala" düğmesiyle başlar. Ortak sürümlü dizi stili, varlık sabitleme kanıtlandıktan sonra gelir.
- **Kürasyon kaydı, metrikler ve servis geçmişi** proje (bölüm) başına kalır; dizi raporu bunları toplar.

## Onay istediğim noktalar

1. Bölüm = mevcut proje eşlemesi.
2. Varlık sürüm sabitleme (`pinned_version`), "son sürümü kullan" varsayılanıyla.
3. Kabul kapısının bölümü otomatik kilitlemesi ("sürüm sabit").
