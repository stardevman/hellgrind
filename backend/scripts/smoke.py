"""Uçtan uca duman testi: tamamen ücretsiz (DRY_RUN=1), gerçek HTTP ve gerçek worker ile.

Kullanım: docker compose exec api python -m scripts.smoke
Kontroller:
  1. DRY_RUN açık ve ücretli çağrılar engelli
  R. Referans eksiksizliği (Faz 12): varlıkları kilitlenmemiş çekimler `blocked` olur ve hiçbir şey harcanmaz; dönüş
     videosundan kareler örneklenir; hareket testi onaylanmadan varlık kilitlenemez; kilitlenen varlıkların çekimleri
     üretilir, panelden bağlanan taslak nesneyi içeren çekim kilitlenene dek engelli kalır
  Y. Stil kılavuzu (Faz 13): film altın saat sürümüyle üretilir; çekimlerin promptları aynı öneki taşır
  B. Storyboard (Faz 14): kare onayı zorunluyken onaysız çekimde hiçbir varyasyon üretilmez; PDF tüm çekimleri sırayla
     içerir; taslak geçiş yalnızca test turu çalıştırır ve kaba kurguyu kendiliğinden birleştirir; iyileştirme geçişi
     yalnızca işaretli çekimi tam tura alır
  2. Örnek senaryo preview ön ayarıyla 4 sahneye ayrılır, hepsi `done` olur; her sahnede rafine havuz 1-3 klip,
     siyah ve donmuş klipler elenmiştir (kusur gerçeği ile kapının bulduğu karşılaştırılır)
  3. Yalnızca müzik değişince planda yalnızca MUX vardır ve harcama değişmez
  4. Küratör seçimi yalnızca LIPSYNC ve MUX çalıştırır
  5. Değişiklik yoksa plan boştur
  6. Film 4 sahneyle birleşir; sekans müziği çekimler arasında kesilmeden akar
  M. Sekans müziği değişince hiçbir sahne yeniden üretilmez, yalnızca film yeniden birleştirilir (Faz 11)
  S. Stil değişikliği: önizleme 4 çekimi ve sanal maliyeti listeler, onay olmadan hiçbir şey üretilmez; etkinleştirince
     çekimler yeni önekle yeniden üretilir
  D. Dış çekim üretim harcaması olmadan `done` olur; rafine #1'in elle düzeltmesi yalnızca LIPSYNC ve MUX çalıştırır (Faz 15)
  A. 2D dizi: yeni stil sürümü 2D başlar ve video istemine 2D cümlesi girer; 3D sürümü önizlemeden geçer; kare tutma
     (on twos) açılıp kapanınca yalnızca GRADE ve MUX çalışır, harcama değişmez
  O. Ses-önce: geçici ses ölçülür; final kayıt dosya adıyla repliğe bağlanır; voice_first açıkken kilitsiz sekansta
     görüntü üretilmez; kilit süreyi sesten hesaplar ve replik düzenlemeyi kapatır; animatik sesle birleşir
  L. Müşteri onay döngüsü: film v001 olarak gönderilir; belirteçli sayfa bütçe ve motor göstermez; not çekime bağlanır;
     değişiklik isteği revizyon turudur; v002 onaylanır; kapatılan bağlantı 410 döner; kararlar kürasyon kaydında
  H. Renk betiği: her sekansın baskın paleti ve çekim paletleri, stil kılavuzunun hedef paleti ve hedefe uzaklık
  E. Teslim: şartname (TV, 25 fps, -23 LUFS) final filmi besler; QC raporu 5 kontrolle ve PDF; eksik hak zinciri paketi
     engellemez; paket master (ProRes), izleme kopyası, stem'ler, M&E, SRT, EDL, QC raporu ve hak zincirini içerir
  C. Renk değişince yalnızca GRADE ve MUX çalışır; bilerek farklı renklendirilen çekim süreklilik uyarısı üretir, önerilen
     düzeltme uygulanınca uyarı kalkar (Faz 16)
  K. Kürasyon kayıtları, metrikler ve dışa aktarma (Faz 17): her panel eylemi bir kürasyon olayı üretir; metrikler
     panelin gördüğü sayılarla tutarlıdır; EDL tüm çekimleri doğru in/out değerleriyle içerir; paket zip'i açılır ve
     stem'ler çekim süresindedir; baştan üretim isteği de kaydedilir
  G. Servis geçmişi: projenin her sanal çağrısı tek satır; tutarlar harcamayla tutar, her satırın modeli var
  T. Sekans tavanı dolunca yalnızca o sekans durur, diğer sekans üretmeye devam eder (Faz 11)
  7. Küçük bütçeli (0,05 USD; MiniMax H3 bir klibi birkaç sentlik GPU süresiyle üretir) projede sahne `frozen`, proje
     `paused` olur; harcama bütçeyi aşmaz, yeni render isteği 423 döner
  P. Yinelenen RunPod gönderimi yok (scripts/smoke_runpod.py): 4 çekim, sahte RunPod; aynı işler yeniden teslim edilince
     yenisi gönderilmez, 3 dk boyunca iş sayısı sabit kalır ve proje sınırını aşmaz; Kill-Switch bekleyen işleri iptal eder
  Q. Kuyruk sırası: kare + video karışık 4 çekimde RunPod'a önce kareler (Qwen-Image, Qwen-Image-Edit) sonra videolar
     gider, en çok 2 model değişimi
  Z. Anahtar kare zinciri: 4 çekimlik continuous sekansta kareler sırayla, her biri öncekinden çizilir; video
     isteklerinde last_frame (sonraki çekimin karesi) vardır, son çekimde yoktur
Duman testi geliştirme veritabanına "duman-testi" başlıklı projeler ekler; hiçbir şey silmez.
"""
import os
import sys
import time

import httpx

API = os.environ.get("SMOKE_API", "http://localhost:8000/api")
c = httpx.Client(base_url=API, timeout=60)
FAILS: list[str] = []
TERMINAL = {"done", "needs_review", "failed", "frozen", "blocked", "board_review", "plan_review"}
CODES = ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]
BUDGET = 200.0       # film + stil değişikliğiyle yeniden üretim ~50 sanal USD; hız bekçisi bütçenin yarısı


def check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


def overview(pid: str) -> dict:
    return c.get(f"/projects/{pid}/overview").json()


def wait_scenes(pid: str, timeout: float = 1200) -> dict:
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        ov = overview(pid)
        states = " ".join(f"{s['scene_code']}:{s['status']}" for s in ov["scenes"])
        if states != last:
            print(f"    {states}")
            last = states
        if ov["scenes"] and all(s["status"] in TERMINAL for s in ov["scenes"]):
            return ov
        time.sleep(2)
    raise TimeoutError(f"{timeout:.0f} sn içinde bitmedi")


def wait_scene(sid: str, changed_from: str | None = None, timeout: float = 300) -> dict:
    """Sahne bitene dek bekler; changed_from verilirse final videonun değişmesini de bekler."""
    t0 = time.time()
    time.sleep(1)
    while time.time() - t0 < timeout:
        sc = c.get(f"/scenes/{sid}").json()
        if sc["status"] in TERMINAL and (changed_from is None or sc["final_video_key"] != changed_from):
            return sc
        time.sleep(1)
    raise TimeoutError(sid)


# MiniMax H3 (kendi GPU sunucumuz) bir klibi birkaç sentlik GPU süresiyle üretir (sanal): tavanlar birkaç klipte dolar.
TINY_BUDGET = 0.05
SEQUENCE_CAP = 0.10


def spent(pid: str) -> float:
    return c.get(f"/projects/{pid}/budget").json()["spent"]


def new_project(title: str, budget: float, **cfg) -> str:
    """Ana akış ve bütçe kontrolleri MiniMax H3 (kendi barındırdığımız; ücretsiz modda yerel motor) ile kurulmuştur:
    `draft_engine="h3"`. fal.ai varsayılanları (H3 Max taslak, Seedance 2.5 final) ayrı bölümde (check_fal) sınanır."""
    cfg = {"draft_engine": "h3", **cfg}
    preset = c.get("/meta").json()["presets"]["preview"]
    # Panelin formundaki gibi: hız bekçisi bütçenin yarısı. Yerel motor saniyeler içinde üretir; varsayılan 10 dakikalık
    # sınır, pahalı bir motor seçilmiş çekimlerde filmi ortasında dondurabilirdi.
    r = c.post("/projects", json={"title": f"{title} {int(time.time())}", "budget_limit_usd": budget,
                                  "settings": {**preset, "max_spend_per_10min": budget / 2, **cfg}})
    r.raise_for_status()
    return r.json()["id"]


def admin_login() -> None:
    """Yönetici şifresi tanımlıysa servis geçmişi ve ayar uçları için giriş yapılır (şifre API'yle aynı .env'den)."""
    from app import admin
    if admin.required():
        c.post("/admin/login", json={"password": admin.password()}).raise_for_status()


def main() -> int:
    t0 = time.time()
    admin_login()

    print("1) Güvenlik")
    meta = c.get("/meta").json()
    check(meta["dry_run"] is True, "DRY_RUN açık")
    if meta["dry_run"] is not True:                 # ücretli moddaki yığında üretim gerçek para harcar: hiçbir şey başlatma
        print("Ücretli mod açık: duman testi durduruldu (önce panelden ücretsiz moda dönün). Hiçbir proje oluşturulmadı.")
        return 2
    from app.live import PAID_KEYS, PaidCallBlocked, guard
    blocked = 0
    for key in PAID_KEYS:
        try:
            guard(key)
        except PaidCallBlocked:
            blocked += 1
    check(blocked == len(PAID_KEYS), f"ücretli çağrıların hepsi engelli ({blocked}/{len(PAID_KEYS)} anahtar)")

    print("R) Referans eksiksizliği: varlıklar kilitlenmeden üretim yok")
    pid = new_project("duman-testi", BUDGET, require_board_approval=True)
    check(c.get(f"/projects/{pid}").json()["settings"]["lint_strictness"] == "block", "yeni proje eksik referansta üretimi engeller")
    r = c.post(f"/projects/{pid}/script", json={"text": meta["sample_script"]})
    r.raise_for_status()
    check(r.json()["shots"] == 4 and r.json()["cast"] == ["Kerem", "Ayşe"], f"senaryo 4 çekime ve {r.json()['cast']} kadrosuna ayrıldı")
    check_sequences(pid)
    check(sorted(r.json()["blocked"]) == CODES, f"4 çekimin hepsi engellendi: {r.json()['blocked']}")
    ov = overview(pid)
    check({s["engine"] for s in ov["scenes"]} == {"h3"}, f"bütün çekimler MiniMax H3'e yönlendi: {sorted({s['engine'] for s in ov['scenes']})}")
    check(all(s["status"] == "blocked" for s in ov["scenes"]) and spent(pid) == 0, "çekimler blocked, harcama 0")
    check(all(s["last_error"].startswith("Üretim engellendi") for s in ov["scenes"]), "eksikler Türkçe yazıldı: " + ov["scenes"][0]["last_error"][:90])
    style = c.get(f"/projects/{pid}/style").json()
    v1 = c.post(f"/projects/{pid}/style", json=style["example"]).json()["version"]
    impact = c.post(f"/projects/{pid}/style/preview", json={"version": v1}).json()
    check(impact["affected"] == [] and impact["pending"] == CODES, "henüz üretilmemiş çekimler için stil önizlemesi yeniden üretim göstermiyor")
    c.post(f"/projects/{pid}/style/activate", json={"version": v1}).raise_for_status()
    assets = {a["name"]: a for a in c.get(f"/projects/{pid}/assets").json()}
    check(sorted(assets) == sorted(["Kerem", "Ayşe", "Terk Edilmiş Dökümhane", "Çöken Köprü"]), f"varlıklar taslak olarak oluştu: {sorted(assets)}")
    prepare_assets(assets)
    ids = {s["scene_code"]: s["id"] for s in overview(pid)["scenes"]}
    fener = c.post(f"/scenes/{ids['S001_SH02']}/assets", json={"name": "Fener"}).json()["asset"]
    r = c.post(f"/projects/{pid}/render-all").json()
    check(r["blocked"] == {"S001_SH02": ["Fener"]} and r["awaiting_board"] == ["S001_SH01", "S002_SH01", "S002_SH02"],
          f"kilitli varlıklı 3 çekimin storyboard karesi çiziliyor, taslak fenerli çekim engelli: {r['blocked']}")
    ov = wait_scenes(pid)
    blocked = next(s for s in ov["scenes"] if s["scene_code"] == "S001_SH02")
    check(blocked["status"] == "blocked" and blocked["spent_usd"] == 0 and not blocked["variations"],
          "referansı eksik nesneli çekim blocked, hiçbir harcama ve varyasyon yok")
    lock_asset(fener)
    r = c.post(f"/scenes/{ids['S001_SH02']}/render").json()
    check(r == {"stages": ["STORYBOARD"], "awaiting_board": True}, f"fener kilitlenince çekimin karesi çiziliyor: {r}")

    print("B) Storyboard onayı, taslak geçiş ve kaba kurgu")
    ov = wait_scenes(pid)
    check(all(s["status"] == "board_review" and s["board_url"] for s in ov["scenes"]),
          "dört çekimin karesi çizildi, hepsi yönetmen onayı bekliyor")
    check(variation_count(pid) == 0, "onaysız çekimlerde hiçbir varyasyon üretilmedi")
    pdf = c.get(f"/projects/{pid}/storyboard.pdf")
    check(pdf.headers["content-type"] == "application/pdf" and pdf_subject(pdf.content) == CODES,
          f"storyboard PDF'i dört çekimi sırayla içeriyor ({pdf_pages(pdf.content)} sayfa, sekans başına bir sayfa)")
    c.post(f"/scenes/{ids['S001_SH02']}/board", json={"status": "changes_requested", "note": "Fener daha parlak"})
    r = c.post(f"/projects/{pid}/draft-pass").json()
    check(r["drafted"] == [] and len(r["awaiting_board"]) == 4, "onaylanmadan taslak geçiş de görüntü üretmedi")
    for sid in ids.values():
        c.post(f"/scenes/{sid}/board", json={"status": "approved"}).raise_for_status()
    r = c.post(f"/projects/{pid}/draft-pass").json()
    check(r["drafted"] == CODES, f"onaylanan dört çekim taslak geçişe girdi: {r['drafted']}")
    ov = wait_scenes(pid)
    tiers = {v["tier"] for s in ov["scenes"] for v in s["variations"]}
    check(all(s["status"] == "done" and s["cut_tier"] == "draft" for s in ov["scenes"]) and tiers == {"probe"},
          f"taslak geçişte tam tur hiç çalışmadı (varyasyon katmanları: {sorted(tiers)})")
    rough = None
    for _ in range(60):
        rough = overview(pid)["draft_timeline"]
        if rough and rough["cut"]:
            break
        time.sleep(1)
    check(bool(rough and rough["cut"]) and [x["scene_code"] for x in rough["cut"]["clips"]] == CODES,
          "kaba kurgu kendiliğinden birleştirildi")
    c.post(f"/scenes/{ids['S002_SH01']}/upgrade", json={"requested": True}).raise_for_status()
    r = c.post(f"/projects/{pid}/upgrade-pass").json()
    check(r["planned"] == {"S002_SH01": ["VISUAL", "LIPSYNC", "MUX"]}, f"iyileştirme geçişi yalnızca işaretli çekimi aldı: {r['planned']}")
    ov = wait_scenes(pid)
    full = sorted({s["scene_code"] for s in ov["scenes"] for v in s["variations"] if v["tier"] == "full"})
    upgraded = next(s for s in ov["scenes"] if s["scene_code"] == "S002_SH01")
    check(full == ["S002_SH01"] and upgraded["cut_tier"] == "final" and upgraded["draft_url"],
          "yalnızca işaretli çekim tam tura girdi; taslak klibi kaba kurgu için korundu")

    print("2) Senaryo -> sahneler -> varyasyonlar -> Quality Gate -> Rafine Havuz -> ses -> montaj")
    ov = wait_scenes(pid)
    scenes = {s["scene_code"]: s for s in ov["scenes"]}
    check(list(scenes) == CODES, f"sahneler: {list(scenes)}")
    check(all(s["status"] == "done" for s in scenes.values()), "tüm sahneler done: " + ", ".join(f"{k}={v['status']}" for k, v in scenes.items()))
    for code, s in scenes.items():
        pool = sorted(v["rank"] for v in s["variations"] if v["rank"])
        check(1 <= len(pool) <= 3 and pool == list(range(1, len(pool) + 1)), f"{code}: rafine havuz sıraları {pool}")
    check_defects(pid)
    spend_after_film = spent(pid)
    check(spend_after_film > 0, f"sanal harcama kaydedildi: ${spend_after_film:.2f}")
    prefix = c.get(f"/projects/{pid}/style").json()["active"]["prefix"]
    check(style_prefixes(pid, ["S001_SH01", "S002_SH01"]) == {prefix}, "iki farklı çekimin promptları aynı stil önekini taşıyor")

    print("3) Stateless Re-render: yalnızca müzik değişir")
    sid = scenes["S001_SH01"]["id"]
    before_final = c.get(f"/scenes/{sid}").json()["final_video_key"]
    r = c.put(f"/scenes/{sid}/audio", json={"kind": "music", "emotion": "joy", "gain_db": -14})
    check(r.json()["stages"] == ["MUX"], f"plan yalnızca MUX: {r.json()['stages']}")
    wait_scene(sid, before_final)
    check(spent(pid) == spend_after_film, "harcama değişmedi (görüntü motoru çağrılmadı)")

    print("4) Küratör seçimi")
    sid = scenes["S002_SH01"]["id"]
    second = next(v for v in next(s for s in overview(pid)["scenes"] if s["id"] == sid)["variations"] if v["rank"] == 2)
    before_final = c.get(f"/scenes/{sid}").json()["final_video_key"]
    r = c.post(f"/scenes/{sid}/promote/{second['id']}")
    check(r.json()["stages"] == ["LIPSYNC", "MUX"], f"yalnızca LIPSYNC + MUX: {r.json()['stages']}")
    wait_scene(sid, before_final)
    check(spent(pid) == spend_after_film, "harcama değişmedi")

    print("5) Değişiklik yok")
    plans = [c.post(f"/scenes/{s['id']}/render").json()["stages"] for s in scenes.values()]
    check(all(p == [] for p in plans), f"her sahnede plan boş: {plans}")
    again = c.post(f"/projects/{pid}/script", json={"text": meta["sample_script"]}).json()
    check(all(p == [] for p in again["planned"].values()) and spent(pid) == spend_after_film, "aynı senaryoyu yeniden yüklemek hiçbir iş planlamadı")

    print("6) Film")
    done = sum(s["status"] == "done" for s in overview(pid)["scenes"])
    r = c.post(f"/projects/{pid}/assemble")
    check(r.json() == {"queued": True, "scenes": done}, f"{done} sahne birleştirmeye alındı")
    film = None
    for _ in range(120):
        film = overview(pid)["timeline"]
        if film and film["cut"]:
            break
        time.sleep(2)
    clips = film["cut"]["clips"] if film and film["cut"] else []
    check([x["scene_code"] for x in clips] == CODES, f"film {len(clips)} sahneyle birleşti, {film['cut']['duration_s'] if film and film['cut'] else '?'} sn")
    tracks = film["cut"].get("tracks", []) if film and film["cut"] else []
    check([t["sequence"] for t in tracks] == ["SEQ01", "SEQ02"], "sekans müzikleri sekans aralıklarına yerleştirildi")
    cut_db = audio_db_around(film["url"] if film else None, clips[1]["in_s"] if len(clips) > 1 else 0)
    check(cut_db > -45, f"SEQ01 içindeki kesmede müzik sürüyor (ses düzeyi {cut_db:.1f} dB)")

    print("M) Sekans müziği değişir: yalnızca film yeniden birleştirilir")
    finals = {sc["scene_code"]: c.get(f"/scenes/{sc['id']}").json()["final_video_key"] for sc in overview(pid)["scenes"]}
    seq1 = next(q for q in overview(pid)["sequences"] if q["code"] == "SEQ01")
    before_film, before_spent = overview(pid)["timeline"]["url"], spent(pid)
    r = c.post(f"/sequences/{seq1['id']}/tracks", json={"mood": "joy", "gain_db": -10}).json()
    check(r["stages"] == ["ASSEMBLE"], f"plan yalnızca film birleştirme: {r['stages']}")
    for _ in range(120):
        film = overview(pid)["timeline"]
        if film["url"] != before_film and film["cut"]:
            break
        time.sleep(1)
    check(film["url"] != before_film and next(t for t in film["cut"]["tracks"] if t["sequence"] == "SEQ01")["mood"] == "joy",
          "film yeni sekans müziğiyle yeniden birleştirildi")
    plans_now = [c.post(f"/scenes/{sc['id']}/render").json()["stages"] for sc in overview(pid)["scenes"]]
    after = {sc["scene_code"]: c.get(f"/scenes/{sc['id']}").json()["final_video_key"] for sc in overview(pid)["scenes"]}
    check(plans_now == [[]] * 4 and after == finals and spent(pid) == before_spent,
          "hiçbir sahne yeniden üretilmedi, sahne videoları ve harcama aynı")

    print("S) Stil değişikliği: önce önizleme, onay olmadan üretim yok")
    v2 = c.post(f"/projects/{pid}/style", json={**c.get(f"/projects/{pid}/style").json()["example"],
                                                "lighting_prefix": "Cold moonlight night, deep blue shadows."}).json()["version"]
    before = (spent(pid), variation_count(pid))
    impact = c.post(f"/projects/{pid}/style/preview", json={"version": v2}).json()
    check([a["scene_code"] for a in impact["affected"]] == CODES and impact["expected_usd"] > 0,
          f"önizleme 4 çekimi listeledi, tahmini sanal maliyet ${impact['expected_usd']:.2f} (en kötü ${impact['worst_usd']:.2f})")
    time.sleep(3)
    check((spent(pid), variation_count(pid)) == before and c.get(f"/projects/{pid}/style").json()["active"]["version"] == v1,
          "önizlemeden sonra harcama ve varyasyon sayısı değişmedi, etkin sürüm aynı")
    r = c.post(f"/projects/{pid}/style/activate", json={"version": v2, "render": True}).json()
    check(all(r["planned"][k] == ["STORYBOARD"] for k in CODES) and r["awaiting_board"] == CODES,
          "etkinleştirince 4 çekimin yeni stille karesi çizildi; görüntü yeniden onay bekliyor")
    wait_scenes(pid)
    for sc in overview(pid)["scenes"]:
        check(sc["board_status"] == "pending", f"{sc['scene_code']}: yeni kare yeniden onay istiyor")
        c.post(f"/scenes/{sc['id']}/board", json={"status": "approved"}).raise_for_status()
    r = c.post(f"/projects/{pid}/render-all").json()
    check(all(r["planned"][k] == ["VISUAL", "LIPSYNC", "MUX"] for k in CODES), "onaydan sonra 4 çekim yeni stille üretime girdi")
    ov = wait_scenes(pid)
    check(all(sc["status"] == "done" for sc in ov["scenes"]) and spent(pid) > before[0], "çekimler yeni stille yeniden üretildi")
    prefix = c.get(f"/projects/{pid}/style").json()["active"]["prefix"]
    check(prefix.startswith("Cold moonlight") and style_prefixes(pid, CODES, latest=True) == {prefix},
          "yeni varyasyonların hepsi yeni stil önekini taşıyor")

    print("D) Dış çekim ve elle düzeltme")
    import tempfile

    from app.engine.procedural import render_clip
    ids = {s["scene_code"]: s["id"] for s in overview(pid)["scenes"]}
    before_spent = spent(pid)
    with tempfile.TemporaryDirectory() as tmp:
        clip = render_clip(os.path.join(tmp, "dis_cekim.mp4"), 11, 640, 360, 3.0, 12, emotion="fear",
                           camera={"shot": "wide", "movement": "tracking"}, characters=["Ayşe", "Kerem"], label="dış çekim")
        with open(clip, "rb") as f:
            data = f.read()
    r = c.post(f"/scenes/{ids['S002_SH02']}/external", params={"filename": "dis_cekim.mp4"}, content=data,
               headers={"content-type": "video/mp4"}).json()
    check(r.get("stages") == ["LIPSYNC", "MUX"], f"dış çekim yüklendi, görüntü üretilmiyor: {r.get('stages')}")
    sc = wait_scene(ids["S002_SH02"])
    check(sc["status"] == "done" and sc["method"] == "external" and spent(pid) == before_spent,
          "dış çekim üretim harcaması olmadan done oldu")
    winner = next(v for v in next(x for x in overview(pid)["scenes"] if x["scene_code"] == "S001_SH01")["variations"]
                  if v["rank"] == 1)
    before_final = c.get(f"/scenes/{ids['S001_SH01']}").json()["final_video_key"]
    with tempfile.TemporaryDirectory() as tmp:
        edit = render_clip(os.path.join(tmp, "duzeltme.mp4"), 12, 640, 360, 3.0, 12, emotion="tension",
                           camera={"shot": "wide", "movement": "pan"}, characters=["Kerem"], label="elle düzeltme")
        with open(edit, "rb") as f:
            data = f.read()
    r = c.post(f"/variations/{winner['id']}/revisions", params={"filename": "duzeltme.mp4", "note": "renk düzeltmesi"},
               content=data, headers={"content-type": "video/mp4"}).json()
    check(r.get("stages") == ["LIPSYNC", "MUX"] and r["revision"]["version"] == 1,
          f"rafine #1'in revizyonu yalnızca LIPSYNC + MUX çalıştırıyor: {r.get('stages')}")
    sc = wait_scene(ids["S001_SH01"], before_final)
    check(f"_r{r['revision']['id']}_" in sc["lipsync_video_key"] and spent(pid) == before_spent,
          "dudak senkronu revizyonla yeniden yapıldı, harcama değişmedi")

    print("C) Renk aşaması ve süreklilik")
    sid = ids["S001_SH02"]
    before = c.get(f"/scenes/{sid}").json()
    before_spent = spent(pid)
    # Bilinçli sapma: önceki çekimden zaten parlaksa daha parlak ve sıcak, karanlıksa daha karanlık ve soğuk.
    current = wait_pair(pid, "S001_SH02", lambda p: p["final"][1] == before["final_video_key"]) or {"delta": {"brightness": 0, "warmth": 0}}
    off = {"exposure": 1.5 if current["delta"]["brightness"] >= 0 else -1.5,
           "temperature": 3000 if current["delta"]["warmth"] >= 0 else 12000}
    r = c.post(f"/scenes/{sid}/grade", json=off).json()
    check(r["stages"] == ["GRADE", "MUX"], f"renk değişince yalnızca GRADE + MUX: {r['stages']}")
    after = wait_scene(sid, before["final_video_key"])
    check(after["lipsync_video_key"] == before["lipsync_video_key"] and after["graded_video_key"] and spent(pid) == before_spent,
          "görüntü ve dudak senkronu çalışmadı, harcama değişmedi")
    pair = wait_pair(pid, "S001_SH02", lambda p: p["final"][1] == after["final_video_key"])   # yeni montajın ölçümü
    check(bool(pair and pair["jump"] and pair["suggestion"]),
          f"bilerek farklı renklendirilen çekim uyarı üretti ({pair and pair['delta']})")
    if pair and pair["suggestion"]:
        before, jumped = c.get(f"/scenes/{sid}").json(), pair
        r = c.post(f"/scenes/{sid}/grade", json=pair["suggestion"]).json()
        fixed = wait_scene(sid, before["final_video_key"])
        pair = wait_pair(pid, "S001_SH02", lambda p: p["final"][1] == fixed["final_video_key"])
        # Karakter dışı bölgenin parlaklığı düzelir (ΔL <= 3). Prosedürel çekimlerde kalan fark içerikten gelen yeşil-macenta
        # (a) farkıdır; renk ayarının bunu düzelten parametresi yok, bu yüzden uyarı yerine skorun yarıya inmesi beklenir.
        check(bool(pair) and (not pair["jump"] or (abs(pair["lab"]["L"]) <= 3 and pair["score"] < jumped["score"] / 2)),
              f"önerilen düzeltme parlaklığı eşledi, uyarı kalktı ya da skor yarıya indi "
              f"(skor {jumped['score']} -> {pair and pair['score']}, ΔL {pair and pair['lab']['L']})")

    check_series_style(pid)
    check_voice_first(meta)
    check_client_review(pid)
    check_curation_and_exports(pid)
    check_history(pid)
    check_color_script(pid)
    check_delivery(pid)                             # şartnameyi 25 fps'e çeker: EDL denetiminden (K) sonra
    check_series_hierarchy(meta)

    print("T) Sekans tavanı: yalnızca o sekans durur")
    capped = new_project("duman-testi-sekans-tavani", 100.0, lint_strictness="off")
    c.post(f"/projects/{capped}/script", json={"text": meta["sample_script"], "auto_render": False}).raise_for_status()
    seqs = {q["code"]: q for q in c.get(f"/projects/{capped}/sequences").json()}
    c.patch(f"/sequences/{seqs['SEQ01']['id']}", json={"budget_cap_usd": SEQUENCE_CAP}).raise_for_status()
    c.post(f"/projects/{capped}/render-all").raise_for_status()
    ov = wait_scenes(capped, timeout=300)
    status = {s["scene_code"]: s["status"] for s in ov["scenes"]}
    seqs = {q["code"]: q for q in ov["sequences"]}
    check(status["S002_SH01"] == status["S002_SH02"] == "done", f"SEQ02 bitti: {status}")
    check("frozen" in (status["S001_SH01"], status["S001_SH02"]) and seqs["SEQ01"]["status"] == "capped",
          f"SEQ01 tavanda durdu (harcama ${seqs['SEQ01']['spent_usd']:.3f} / tavan ${SEQUENCE_CAP})")
    check(seqs["SEQ01"]["spent_usd"] + seqs["SEQ01"]["reserved_usd"] <= SEQUENCE_CAP, "sekans harcaması tavanı aşmadı")
    check(ov["project"]["status"] == "producing" and not ov["project"]["frozen"]
          and any(e["kind"] == "sequence_capped" for e in ov["events"]), "proje durmadı, olay yazıldı")

    print(f"7) Kill-Switch: {TINY_BUDGET} USD'lik proje")
    tiny = new_project("duman-testi-kucuk-butce", TINY_BUDGET, lint_strictness="off")     # yalnızca bütçe sınanır
    c.post(f"/projects/{tiny}/script", json={"text": meta["sample_script"]}).raise_for_status()
    ov = wait_scenes(tiny, timeout=300)
    project = ov["project"]
    check("frozen" in {s["status"] for s in ov["scenes"]}, "bir sahne frozen oldu")
    check(project["status"] == "paused" and project["frozen"], f"proje paused ve dondurulmuş (durum: {project['status']})")
    check(project["spent_usd"] + project["reserved_usd"] <= TINY_BUDGET, f"harcama {TINY_BUDGET} USD'yi aşmadı (${project['spent_usd']:.4f} + ayrılan ${project['reserved_usd']:.4f})")
    r = c.post(f"/scenes/{ov['scenes'][0]['id']}/render")
    check(r.status_code == 423, f"yeni render isteği 423 döndü ({r.status_code})")

    check_fal(meta)

    from scripts import smoke_runpod                # en sonda: süreç içinde, sahte RunPod'la (ağ yok)
    smoke_runpod.run(c, check, new_project, meta["sample_script"])
    smoke_runpod.run_order(c, check, new_project, meta["sample_script"])
    smoke_runpod.run_chain(c, check, new_project)

    print(f"\n{time.time() - t0:.0f} sn")
    if FAILS:
        print("BAŞARISIZ KONTROLLER:\n - " + "\n - ".join(FAILS))
        return 1
    print("TÜM KONTROLLER GEÇTİ")
    return 0


def wait_for(pid: str, cond, timeout: float = 300) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond(overview(pid)):
            return True
        time.sleep(2)
    return False


def give_references(pid: str) -> None:
    """Projenin bütün varlıklarına yerel taslak referans (yönetmenin görsel yüklemesi yerine; ücretsiz)."""
    for a in c.get(f"/projects/{pid}/assets").json():
        c.post(f"/assets/{a['id']}/references/placeholder").raise_for_status()


def check_fal(meta: dict) -> None:
    """fal.ai varsayılanları ücretsiz modda: yeni çekimler taslak motoruna (H3 Max) gider, çekim başına tek klip; kuru
    çalıştırma fal'e gitmeden şemaya uygun gövde verir; "Seedance 2.5 ile yükselt" aynı çekimi final motoruyla üretir,
    eski klip varyasyon olarak kalır."""
    print("V) fal.ai motorları: referans + senaryo, tüm sahne tek istekte; taslak H3 Max, Seedance 2.5 ile yükselt (sanal)")
    check(meta["h3_platform"] == "kapali" and meta["video"]["draft_engine"] == "h3max"
          and meta["video"]["final_engine"] == "seedance25", "RunPod kapalı; taslak h3max, final seedance25")
    check(meta["video_input_mode"] == "reference" and meta["scene_render_mode"] == "whole_scene",
          "varsayılan: referans modu (kare üretimi yok), tüm sahne tek istekte")
    pid = new_project("duman-testi-fal", 100.0, lint_strictness="off", draft_engine=None, fal_clip_attempts=3)
    r = c.post(f"/projects/{pid}/script", json={"text": meta["sample_script"]})
    r.raise_for_status()
    check(sorted(r.json()["blocked"]) == CODES, "referans görseli olmayan varlıklarla hiçbir çekim üretilmez (eksik referans)")
    give_references(pid)
    c.post(f"/projects/{pid}/render-all").raise_for_status()
    ov = wait_scenes(pid)
    check({s["engine"] for s in ov["scenes"]} == {"h3max"}, "yeni çekimler H3 Max'e yönlendi")
    check(all(s["status"] == "done" and s["cut_tier"] == "draft" for s in ov["scenes"]),
          "dört çekim taslak olarak bitti: " + " ".join(f"{s['scene_code']}:{s['status']}" for s in ov["scenes"]))
    check(all(len([v for v in s["variations"] if v["rank"]]) == 1 for s in ov["scenes"]), "çekim başına tek klip havuzda")
    check(all(s["board_url"] is None for s in ov["scenes"]), "hiçbir storyboard karesi çizilmedi (referans modu)")
    parts = {tuple(v["part"] or ()) for s in ov["scenes"] for v in s["variations"] if v["rank"]}
    check(len(parts) < len(ov["scenes"]) and all(len(p) >= 1 for p in parts),
          f"sahnenin çekimleri tek istekte üretilip kesildi: {sorted(parts)}")
    check(all((v.get("gates") or {}).get("step") == 0.5 for s in ov["scenes"] for v in s["variations"] if v["rank"]),
          "her klipte video kapıları 0,5 sn'de bir örnekle kayıtlı")
    plan = c.get(f"/projects/{pid}/render-plan", params={"dry_run": "true"}).json()
    rows = plan["scenes"]
    check(plan["network"] is False and all(r["endpoint"].startswith("minimax/h3-max/") for r in rows),
          f"kuru çalıştırma: {len(rows)} çekim H3 Max uç noktasına, ağ yok")
    check(all(isinstance(r["body"]["duration"], (int, float)) and r["body"]["prompt_expansion_mode"] == "disabled"
              and r["body"]["resolution"] == "480P" for r in rows),
          "H3 Max gövdesi: 480P (en ucuz), süre sayı, prompt_expansion_mode zorunlu alan dolu")
    check(plan["upgrade_total_usd"] > plan["total_usd"] > 0 and plan["requests"] == len(parts),
          f"tahmin: {plan['requests']} istek, taslak {plan['total_usd']:.2f} $, yükseltme {plan['upgrade_total_usd']:.2f} $")
    sc = ov["scenes"][0]
    before, draft_id = spent(pid), next(v["id"] for v in sc["variations"] if v["rank"] == 1)
    previous_final = c.get(f"/scenes/{sc['id']}").json()["final_video_key"]
    up = c.get(f"/scenes/{sc['id']}/fal-plan").json()["upgrade"]
    check(up["available"] and up["scope"] == "scene" and up["budget_ok"],
          f"yükseltme önizlemesi: sahne {up.get('shots')}, toplam {up.get('total_usd', 0):.2f} $")
    stale = c.post(f"/scenes/{sc['id']}/upgrade-final", params={"expected_usd": 0.01})
    check(stale.status_code == 409, "tahmin onaylanandan yüksekse yükseltme reddedilir")
    r = c.post(f"/scenes/{sc['id']}/upgrade-final", params={"expected_usd": up["total_usd"]})
    check(r.status_code == 200 and r.json()["engine"] == "seedance25", f"Seedance 2.5 ile yükselt ({r.status_code})")
    wait_scene(sc["id"], changed_from=previous_final, timeout=600)
    after = next(s for s in overview(pid)["scenes"] if s["id"] == sc["id"])
    winner = next((v for v in after["variations"] if v["rank"] == 1), None)
    old = next((v for v in after["variations"] if v["id"] == draft_id), None)
    check(after["cut_tier"] == "final" and after["engine"] == "seedance25" and winner and winner["id"] != draft_id,
          "yükseltilen çekim final, havuzun başında Seedance 2.5 klibi")
    check(old is not None and old["status"] == "passed", "taslak klip varyasyon olarak kaldı")
    check(spent(pid) > before, f"yükseltme sanal olarak ücretlendi ({spent(pid) - before:.2f} $)")
    dry = c.post(f"/scenes/{sc['id']}/render", params={"dry_run": "true"}).json()
    check(dry["dry_run"] and dry["endpoint"].startswith("bytedance/seedance-2.5/") and isinstance(dry["body"]["duration"], str)
          and dry["body"].get("draft") is True, "kuru çalıştırma: Seedance 2.5 taslağı (draft), süre metin")

    print("X) Taslağı 1080p'ye tamamlama ve upscale")
    d = c.get(f"/scenes/{sc['id']}/draft").json()
    check(d.get("available") and d["label"].startswith("≈") and d["cheaper"]["label"].startswith("Upscale ile 1080p")
          and 0 < d["draft"]["remaining_s"] <= 7 * 24 * 3600,
          f"taslak: {d.get('label')}, daha ucuz: {d.get('cheaper', {}).get('label')}, kalan süre {d.get('draft', {}).get('remaining_s')} sn")
    r = c.post(f"/scenes/{sc['id']}/seedance/complete", params={"expected_usd": d["cost"]["usd"]})
    check(r.status_code == 200, f"taslak tamamlama kuyruğa verildi ({r.status_code})")
    done = wait_for(pid, lambda ov: all(next((v for v in s["variations"] if v["rank"] == 1), {}).get("resolution") == "1080p"
                                        and s["status"] == "done" for s in ov["scenes"] if s["scene_code"] in d["part"]))
    check(done, "tamamlanan parçanın çekimleri 1080p kazananla bitti; taslak kesimleri varyasyon olarak kaldı")
    other = next(s for s in overview(pid)["scenes"] if s["scene_code"] not in d["part"])
    plan = c.post(f"/scenes/{other['id']}/upscale", params={"dry_run": "true"}).json()
    check(plan["items"] and plan["total_usd"] > 0 and len(plan["alternatives"]) == 5,
          f"upscale planı: {plan['endpoint']} {plan['total_usd']:.3f} $ (5 seçenek)")
    r = c.post(f"/scenes/{other['id']}/upscale", params={"expected_usd": plan["total_usd"]})
    check(r.status_code == 200, "upscale kuyruğa verildi")
    up = wait_for(pid, lambda ov: any(v.get("upscaled_from") and v["rank"] == 1 for s in ov["scenes"]
                                      if s["id"] == other["id"] for v in s["variations"])
                  and next(s for s in ov["scenes"] if s["id"] == other["id"])["status"] == "done")
    check(up, "upscale edilen klip kazanan, orijinal varyasyon olarak kaldı, montaj yeniden yapıldı")

    print("W) Plan onayı: onaysız render yok, onaylanınca parça üretilir")
    gated = new_project("duman-testi-plan", 50.0, lint_strictness="off", draft_engine=None, require_plan_approval=True,
                        fal_clip_attempts=3)
    c.post(f"/projects/{gated}/script", json={"text": meta["sample_script"], "auto_render": False}).raise_for_status()
    give_references(gated)
    before = spent(gated)                       # yer tutucu referansların sanal bedeli
    r = c.post(f"/projects/{gated}/render-all")
    check(r.status_code == 200 and sorted(r.json()["awaiting_plan"]) == CODES, f"4 çekim plan onayı bekliyor: {r.json().get('awaiting_plan')}")
    time.sleep(2)
    ov = overview(gated)
    check(all(s["status"] == "plan_review" for s in ov["scenes"]) and spent(gated) == before,
          "plan_review; onaysız video harcaması yok")
    first = ov["scenes"][0]
    plan = c.get(f"/scenes/{first['id']}/fal-plan").json()
    ok = c.post(f"/scenes/{first['id']}/plan/approve", params={"render": "true"}, json={"plan_hash": plan["plan_hash"]})
    check(ok.status_code == 200 and ok.json()["part"] == plan["part"], f"plan onaylandı ve üretime verildi: {plan['part']}")
    for x in [s for s in ov["scenes"] if s["scene_code"] in plan["part"]]:
        wait_scene(x["id"])                     # parçanın her çekimi kendi ses/montaj zincirini bitirir
    done = [s for s in overview(gated)["scenes"] if s["scene_code"] in plan["part"]]
    check(all(s["status"] == "done" for s in done), "onaylanan parçanın çekimleri bitti; diğerleri onay bekliyor (" +
          ", ".join(f"{s['scene_code']}:{s['status']}" for s in done) + ")")


def check_history(pid: str) -> None:
    print("G) Servis geçmişi")
    calls, before = [], None
    while True:
        page = c.get("/history", params={"project_id": pid, "limit": 500, **({"before": before} if before else {})}).json()
        calls += page["items"]
        if not (before := page["next"]):
            break
    total = sum(x["amount_usd"] or 0 for x in calls if x["status"] == "succeeded")
    check(bool(calls) and abs(total - spent(pid)) < 1e-3,
          f"{len(calls)} çağrının işlenen toplamı harcamayla aynı (${total:.4f} / ${spent(pid):.4f})")
    check(all(x["mode"] == "virtual" for x in calls), "hepsi sanal: ücretsiz modda hiçbir servis çağrılmadı")
    check(all(x["model"] and x["service"] in ("runpod", "fal") for x in calls), "her satırda ücretli modda çağrılacak model var")
    h3 = [x for x in calls if x["operation"] in ("video", "motion_test")]
    check(bool(h3) and all(x["service"] == "runpod" and x["model"] == "MiniMaxAI/MiniMax-H3" for x in h3),
          f"video ve hareket testi MiniMax H3'e (RunPod Serverless) yazıldı ({len(h3)} çağrı)")
    rp = c.get("/settings/runpod").json()
    check(rp["platform"] == "kapali" and rp["disabled"] and rp["disabled_message"].startswith("RunPod: kapalı")
          and rp["allowed_datacenters"] == ["CA-MTL-1", "CA-MTL-2", "CA-MTL-3"],
          f"RunPod kapalı (endpoint silindi); izinli bölgeler yine yalnızca Kanada ({', '.join(rp['allowed_datacenters'])})")
    ov = overview(pid)
    modes = {sc["scene_code"]: (sc["h3_mode"], sc["keyframe_model"]) for sc in ov["scenes"]}
    check(all(sc["h3_mode"] == "fl2va" and sc["keyframe_url"] for sc in ov["scenes"])
          and all(sc["keyframe_model"] == ("qwen_edit" if sc["edit_refs"] else "qwen") for sc in ov["scenes"])
          and any(sc["edit_refs"] for sc in ov["scenes"]),
          f"her çekim FL2VA ve ilk karesi var; karakterli çekimlerin karesi Qwen-Image-Edit ile referanstan: {modes}")
    est = c.post("/estimate", json={"project_id": pid, "preset": "economy"}).json()
    generated = [sc["scene_code"] for sc in ov["scenes"] if sc["method"] != "external"]      # dış çekim ücretsizdir
    check([x["code"] for x in est["shots"]] == generated and 0 < est["expected_usd"] <= est["max_usd"]
          and all(x["clips"] == 6 for x in est["shots"]),
          f"ekonomik ön ayarla tahmini maliyet: ${est['expected_usd']:.3f} (en çok ${est['max_usd']:.3f}), "
          f"{len(generated)} üretilen çekim, çekim başına 6 klip")
    ops = {x["operation"] for x in calls}
    check({"video", "storyboard", "reference", "motion_test"} <= ops, f"işlemler: {sorted(ops)}")
    s = c.get("/history/summary", params={"project_id": pid}).json()
    check(s["totals"]["real_usd"] == 0 and abs(s["totals"]["virtual_usd"] - total) < 1e-3, "özet: gerçek harcama 0, sanal toplam tutuyor")
    import csv
    import io
    r = c.get("/history.csv", params={"project_id": pid})
    check(r.status_code == 200 and len(list(csv.reader(io.StringIO(r.text)))) == len(calls) + 1, "CSV her çağrıyı içeriyor")


SPEC_ACTIONS = {"promote", "upgrade_request", "board_approve", "board_reject", "asset_lock", "revision_upload",
                "style_activate", "force_rerender"}


def curation(pid: str) -> list[dict]:
    import json
    return [json.loads(line) for line in c.get(f"/projects/{pid}/curation.jsonl").text.splitlines()]


def check_curation_and_exports(pid: str) -> None:
    import io
    import re
    import wave
    import zipfile

    from app.post.export import frames, timecode

    print("K) Kürasyon kayıtları, metrikler ve dışa aktarma")
    events = curation(pid)
    actions = {e["action"] for e in events}
    check(SPEC_ACTIONS - {"force_rerender"} <= actions,
          f"panel eylemleri kürasyon kaydında ({len(events)} olay; eksik: {sorted(SPEC_ACTIONS - {'force_rerender'} - actions)})")
    reject = next((e for e in events if e["action"] == "board_reject"), None)
    promote = next((e for e in events if e["action"] == "promote"), None)
    check(bool(reject and reject["scene_code"] == "S001_SH02" and reject["payload"]["request"]["body"].get("note") == "Fener daha parlak"
               and promote and promote["variation_id"] and promote["payload"]["variation"]["score"] is not None),
          "kararlar sahne, varyasyon, not ve skor bağlamıyla kaydedildi")

    before_film = overview(pid)["timeline"]["url"]
    c.post(f"/projects/{pid}/assemble").raise_for_status()           # revizyon ve renk sonrası güncel film
    film = None
    for _ in range(120):
        film = overview(pid)["timeline"]
        if film["url"] != before_film and film["cut"]:
            break
        time.sleep(1)
    cut = film["cut"]
    ov = overview(pid)
    m = c.get(f"/projects/{pid}/metrics").json()
    done = sum(sc["status"] == "done" for sc in ov["scenes"])
    generated = [v for sc in ov["scenes"] for v in sc["variations"] if v["source"] != "external"]   # dış çekim üretim değil
    seq_spent = {q["code"]: q["spent_usd"] for q in ov["sequences"]}
    check(m["variations"] == len(generated) and m["accepted_shots"] == done == 4
          and m["variations_per_accepted_shot"] == round(m["variations"] / done, 2),
          f"kabul edilen çekim başına {m['variations_per_accepted_shot']} varyasyon ({m['variations']} / {done})")
    check(abs(m["spent_usd"] - ov["project"]["spent_usd"]) < 1e-3 and m["film_minutes_source"] == "film"
          and abs(m["film_minutes"] - cut["duration_s"] / 60) < 1e-3
          and abs(m["cost_per_film_minute_usd"] - m["spent_usd"] / m["film_minutes"]) < 0.01,
          f"film dakikası başına {m['cost_per_film_minute_usd']} USD (sanal, {m['film_minutes']} dk)")
    check(sum(q["variations"] for q in m["sequences"]) == m["variations"]
          and abs(sum(q["variation_share"] for q in m["sequences"]) - 1) < 0.01
          and all(abs(q["spent_usd"] - seq_spent[q["code"]]) < 1e-3 for q in m["sequences"])
          and sum(m["purge_reasons"].values()) == sum(v["status"] in ("purged", "failed") for v in generated),
          f"sekans payları ve eleme nedenleri tutarlı: {m['purge_reasons']}")
    check(m["probe"]["succeeded"] >= 4 and 0 < m["probe"]["success_rate"] <= 1 and m["draft"]["upgraded"] >= 1,
          f"test turu başarı oranı {m['probe']['success_rate']}, iyileştirilen {m['draft']['upgraded']}/{m['draft']['drafted']}")

    edl = c.get(f"/projects/{pid}/export", params={"format": "edl"}).text
    events_edl = re.findall(r"^(\d{3})  (\S+)\s+AA/V  C\s+(\S+) (\S+) (\S+) (\S+)$", edl, re.M)
    expected = [(timecode(frames(x["in_s"])), timecode(frames(x["out_s"]))) for x in cut["clips"]]
    check(len(events_edl) == len(cut["clips"]) == 4 and [(e[4], e[5]) for e in events_edl] == expected
          and [e[1] for e in events_edl] == [x["scene_code"].replace("_", "") for x in cut["clips"]],
          f"EDL {len(events_edl)} çekimi doğru in/out değerleriyle içeriyor ({expected[0][0]} – {expected[-1][1]})")
    r = c.get(f"/projects/{pid}/export", params={"format": "package"}, timeout=600)
    ok, worst = r.status_code == 200, 0.0
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(z.namelist())
        ok = ok and z.testzip() is None and {"film.edl", "film.otio", "kurasyon.jsonl", "kesme_listesi.json"} <= names
        for n, clip in enumerate(cut["clips"], start=1):
            base = f"{n:03d}_{clip['scene_code']}"
            ok = ok and f"{base}/final.mp4" in names and any(x.startswith(f"{base}/rafine/") for x in names)
            for stem in ("dialogue", "music", "sfx"):
                with wave.open(io.BytesIO(z.read(f"{base}/stems/{stem}.wav"))) as w:
                    worst = max(worst, abs(w.getnframes() / w.getframerate() - (clip["out_s"] - clip["in_s"])))
    except (zipfile.BadZipFile, KeyError) as e:
        ok = False
        print("   paket:", e)
    check(ok and worst < 0.05, f"paket zip'i açılıyor; stem'ler çekim süresinde (en büyük sapma {worst * 1000:.0f} ms)")

    sid = next(sc["id"] for sc in ov["scenes"] if sc["scene_code"] == "S002_SH01")
    before = c.get(f"/scenes/{sid}").json()["final_video_key"]
    r = c.post(f"/scenes/{sid}/render", params={"force_visual": "true"}).json()
    wait_scene(sid, before)
    last = curation(pid)[-1]
    check("VISUAL" in r["stages"] and last["action"] == "force_rerender" and last["scene_code"] == "S002_SH01",
          f"baştan üretim isteği kaydedildi ({r['stages']})")


def check_series_style(pid: str) -> None:
    """2D dizi: stil kılavuzunun çizim türü video istemine girer; tür değişikliği yeni sürümdür ve önizlemeden geçer; kare
    tutma (on twos) yalnızca renk ve montajı çalıştırır, harcama değişmez."""
    print("A) Animasyon dizisi: çizim türü ve kare tutma")
    st = c.get(f"/projects/{pid}/style").json()
    active = st["active"]
    check(active["medium"] == "2d" and "2D animation" in active["video_line"] and "3D" not in active["video_line"],
          f"yeni sürüm 2D başladı; video istemi 2D cümlesini taşıyor: {active['video_line'][-80:]}")
    v3 = c.post(f"/projects/{pid}/style", json={**st["examples"]["3d"], "lighting_prefix": active["lighting_prefix"],
                                                "motion_style": active["motion_style"]}).json()
    before = (spent(pid), variation_count(pid))
    impact = c.post(f"/projects/{pid}/style/preview", json={"version": v3["version"]}).json()
    generated = [sc["scene_code"] for sc in overview(pid)["scenes"] if sc["method"] != "external"]   # dış çekim stilden bağımsız
    affected = [a["scene_code"] for a in impact["affected"]]
    check(v3["medium"] == "3d" and affected == generated and (spent(pid), variation_count(pid)) == before,
          f"3D sürümü yeni bir stil sürümü: önizleme üretilen {len(affected)} çekimi listeledi ({', '.join(affected)}), "
          "onaysız hiçbir şey üretilmedi")
    finals = {sc["id"]: c.get(f"/scenes/{sc['id']}").json()["final_video_key"] for sc in overview(pid)["scenes"]}
    r = c.post(f"/projects/{pid}/on-twos", json={"enabled": True}).json()
    check(sorted(r["regraded"]) == CODES, f"kare tutma açıldı; biten çekimler yalnızca renk ve montajdan geçiyor: {r['regraded']}")
    for sid, key in finals.items():
        wait_scene(sid, key)
    rows = [c.get(f"/scenes/{sid}").json() for sid in finals]
    check(all(x["graded_video_key"] and x["status"] == "done" for x in rows) and (spent(pid), variation_count(pid)) == before,
          "her çekim kare tutmayla yeniden renklendirildi; görüntü motoru çağrılmadı, harcama aynı")
    finals = {x["id"]: x["final_video_key"] for x in rows}
    r = c.post(f"/projects/{pid}/on-twos", json={"enabled": False}).json()
    for sid, key in finals.items():
        wait_scene(sid, key)
    check(sorted(r["regraded"]) == CODES and spent(pid) == before[0], "kare tutma kapatıldı; yine yalnızca renk ve montaj")


def check_voice_first(meta: dict) -> None:
    """Ses-önce: geçici ses ölçülür, final kayıt dosya adıyla repliğe bağlanır, voice_first açıkken kilitsiz sekansta
    görüntü üretilmez, kilit süreyi sesten hesaplar ve replik düzenlemeyi kapatır, animatik sesle birleşir."""
    import subprocess
    import tempfile
    print("O) Ses-önce: geçici ses, final kayıt, kilit, animatik")
    pid = new_project("duman-testi-ses", 100.0, lint_strictness="off", voice_first=True)
    c.post(f"/projects/{pid}/script", json={"text": meta["sample_script"], "auto_render": False}).raise_for_status()
    st = c.get(f"/projects/{pid}/voice").json()
    lines = [(sh, ln) for q in st["sequences"] for sh in q["shots"] for ln in sh["lines"]]
    check(bool(lines) and st["counts"]["final"] == 0 and st["counts"]["unmeasured"] == len(lines),
          f"Ses sayfası {len(lines)} repliği sekans ve çekim altında listeledi; hepsi seslendirilmemiş")
    r = c.post(f"/projects/{pid}/render-all").json()
    check(sorted(r["awaiting_voice"]) == CODES and variation_count(pid) == 0 and spent(pid) == 0,
          "voice_first: ses kilitlenmeden hiçbir görüntü üretilmedi")
    c.post(f"/projects/{pid}/voice/temp").raise_for_status()
    for _ in range(60):
        st = c.get(f"/projects/{pid}/voice").json()
        if st["counts"]["unmeasured"] == 0:
            break
        time.sleep(1)
    lines = [(sh, ln) for q in st["sequences"] for sh in q["shots"] for ln in sh["lines"]]
    check(all(ln["duration_s"] and ln["engine"] == "espeak" for _, ln in lines), "geçici sesler espeak ile üretildi ve ölçüldü")
    shot, line = lines[0]
    name = f"{shot['scene_code']}_{line['n']}.wav"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, name)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=1.8", path], check=True)
        with open(path, "rb") as f:
            up = c.post(f"/projects/{pid}/voice/upload", params={"filename": name}, content=f.read(),
                        headers={"content-type": "audio/wav"}).json()
    c.put(f"/scenes/{shot['scene_id']}/audio", json={"kind": "dialogue", "source": "upload", "file_key": up["key"],
                                                    "replaces": line["track_id"], "params": {"filename": name}}).raise_for_status()
    final = c.get(f"/projects/{pid}/voice").json()["sequences"][0]["shots"][0]["lines"][0]
    check(up["match"] == {"scene_code": shot["scene_code"], "n": line["n"]} and final["final"] and abs(final["duration_s"] - 1.8) < 0.1,
          f"final kayıt dosya adıyla ({name}) repliğe bağlandı, süresi ölçüldü")
    seqs = [q for q in st["sequences"] if q["id"]]
    first = c.post(f"/sequences/{seqs[0]['id']}/voice-lock", json={"locked": True}).json()
    r = c.post(f"/projects/{pid}/render-all").json()
    rest = [sh["scene_code"] for q in seqs[1:] for sh in q["shots"]]
    check(sorted(r["awaiting_voice"]) == sorted(rest) and all("VISUAL" in r["planned"][sh["scene_code"]] for sh in seqs[0]["shots"]),
          f"yalnızca kilitli {seqs[0]['code']} üretime girdi; kilitsiz sekansın çekimleri bekliyor: {r['awaiting_voice']}")
    first_codes = {sh["scene_code"] for sh in seqs[0]["shots"]}
    wait_for(pid, lambda ov: all(x["status"] in TERMINAL for x in ov["scenes"] if x["scene_code"] in first_codes))
    retimed = list(first["retimed"])
    for q in seqs[1:]:
        retimed += c.post(f"/sequences/{q['id']}/voice-lock", json={"locked": True}).json()["retimed"]
    st = c.get(f"/projects/{pid}/voice").json()
    talkers = [sh for q in st["sequences"] for sh in q["shots"] if sh["lines"]]
    check(all(q["locked"] for q in st["sequences"] if q["id"]) and sorted(retimed) == sorted(sh["scene_code"] for sh in talkers)
          and all(sh["duration_source"] == "voice" for sh in talkers), f"kilit: süresi sesten hesaplanan {retimed}")
    blocked = c.put(f"/scenes/{talkers[0]['scene_id']}/audio", json={"kind": "dialogue", "text": "Değişti.",
                                                                     "replaces": talkers[0]["lines"][0]["track_id"]})
    check(blocked.status_code == 409, "kilitli sekansta replik değiştirilemiyor (409)")
    c.post(f"/projects/{pid}/animatic").raise_for_status()
    film = None
    for _ in range(90):
        film = overview(pid)["animatic"]
        if film and film["cut"]:
            break
        time.sleep(1)
    cut = film["cut"] if film else {}
    check(bool(cut) and [x["scene_code"] for x in cut["clips"]] == CODES and cut["voiced"] == len(talkers),
          f"animatik {len(cut.get('clips', []))} çekimle birleşti, {cut.get('duration_s')} sn, {cut.get('voiced')} çekimin süresi sesten")
    r = c.post(f"/projects/{pid}/render-all").json()
    check(all("VISUAL" in r["planned"][k] for k in rest) and not r["awaiting_voice"], "bütün sekanslar kilitlenince kalan çekimler üretime girdi")
    ov = wait_scenes(pid)
    # Ücretsiz modun kalite kapısı rastgeledir (şanssız bir seed kümesi needs_review bırakabilir): ölçülen, görüntü
    # motorunun kilitten sonra her çekimde çalışmasıdır.
    check(all(x["variations"] and x["status"] in ("done", "needs_review") for x in ov["scenes"]),
          "sesten süreli çekimler üretildi: " + ", ".join(f"{x['scene_code']}={x['status']}" for x in ov["scenes"]))
    c.post(f"/sequences/{seqs[0]['id']}/voice-lock", json={"locked": False}).raise_for_status()
    actions = {e["action"] for e in curation(pid)}
    check({"voice_lock", "voice_unlock", "voice_temp", "animatic"} <= actions, "kilit, kilidin açılması ve animatik kürasyon kaydında")


def check_client_review(pid: str) -> None:
    """Müşteri onay döngüsü: film v001 olarak gönderilir; belirteçli sayfa bütçe ve motor göstermez; not çekime bağlanır;
    değişiklik isteği revizyon turudur; yeni sürüm v002 onaylanır; kararlar kürasyon kaydında."""
    print("L) Müşteri onay döngüsü: sürüm, inceleme bağlantısı, not, karar")
    v1 = c.post(f"/projects/{pid}/versions", json={"gate": "final"}).json()
    link = c.post(f"/projects/{pid}/review-links", json={"version_id": v1["id"], "days": 7}).json()
    page = c.get(f"/review/{link['token']}")
    raw = page.text.lower().replace(link["token"].lower(), "")
    check(page.status_code == 200 and v1["label"] == "v001" and link["url"].startswith("/panel/review/?token=")
          and not any(w in raw for w in ("usd", "budget", "bütçe", "engine", "motor", "spent", pid)),
          "v001 için süreli bağlantı oluştu; müşteri sayfası bütçe, motor ve maliyet göstermiyor")
    clips = page.json()["clips"]
    at = round((clips[1]["in_s"] + clips[1]["out_s"]) / 2, 2)
    c.post(f"/review/{link['token']}/notes", json={"at_s": at, "text": "Bu kesmede ışık daha sıcak olsun"}).raise_for_status()
    notes = overview(pid)["client"]["notes"]
    check(notes and notes[-1]["scene_code"] == clips[1]["scene_code"] and not notes[-1]["resolved"],
          f"müşteri notu {at} sn'den {clips[1]['scene_code']} çekimine bağlandı ve Kurgu'da görünüyor")
    c.post(f"/review/{link['token']}/decision", json={"decision": "changes", "note": "Sonu kısalt"}).raise_for_status()
    v2 = c.post(f"/projects/{pid}/versions", json={"gate": "final"}).json()
    link2 = c.post(f"/projects/{pid}/review-links", json={"version_id": v2["id"]}).json()
    c.post(f"/review/{link2['token']}/decision", json={"decision": "approve"}).raise_for_status()
    st = c.get(f"/projects/{pid}/client").json()
    final = next(g for g in st["gates"] if g["gate"] == "final")
    check([(v["label"], v["status"]) for v in final["versions"]] == [("v001", "changes_requested"), ("v002", "approved")]
          and final["rounds"] == 1 and final["status"] == "approved",
          f"değişiklik isteği 1 revizyon turu ({final['rounds']}/{final['limit']}); v002 onaylandı")
    c.patch(f"/client-notes/{notes[-1]['id']}", json={"resolved": True}).raise_for_status()
    c.delete(f"/review-links/{link['id']}").raise_for_status()
    check(c.get(f"/review/{link['token']}").status_code == 410, "kapatılan bağlantı artık açılmıyor (410)")
    actions = [e["action"] for e in curation(pid)]
    check({"client_note", "client_reject", "client_approve", "client_send", "review_link"} <= set(actions),
          "müşteri notu, değişiklik isteği ve onay kürasyon kaydında")


def check_color_script(pid: str) -> None:
    """Renk betiği: her sekansın baskın paleti, çekim paletleri, stil kılavuzunun hedefi ve hedefe uzaklık."""
    print("H) Renk betiği")
    cs = c.get(f"/projects/{pid}/color-script").json()
    target = c.get(f"/projects/{pid}/style").json()["active"]["palette"]
    seqs = cs["sequences"]
    check([q["code"] for q in seqs] == ["SEQ01", "SEQ02"] and all(q["palette"] and q["shots"] for q in seqs)
          and cs["target"] == target and all(q["distance"] is not None for q in seqs),
          "renk betiği: " + "; ".join(f"{q['code']} {' '.join(x['hex'] for x in q['palette'][:3])} (ΔE {q['distance']})" for q in seqs))


def check_delivery(pid: str) -> None:
    """Teslim: şartname final filmi besler (25 fps, hedef loudness); QC raporu ve PDF; hak zinciri; paket içeriği."""
    import io
    import zipfile
    print("E) Teslim şartnamesi, QC, hak zinciri ve paket")
    st = c.get(f"/projects/{pid}/delivery").json()
    tv = {k: v for k, v in st["presets"]["tv_tr"].items() if k != "label"}
    c.put(f"/projects/{pid}/delivery", json={**tv, "resolution": "1280x720", "preset": "tv_tr"}).raise_for_status()
    before = overview(pid)["timeline"]["url"]
    c.post(f"/projects/{pid}/assemble").raise_for_status()
    film = None
    for _ in range(120):
        film = overview(pid)["timeline"]
        if film["url"] != before and film["cut"]:
            break
        time.sleep(1)
    fmt = (film["cut"] or {}).get("format") or {}
    check(film["url"] != before and fmt.get("fps") == 25 and fmt.get("loudness_lufs") == -23.0,
          f"şartname final filmi besledi: {fmt.get('width')}x{fmt.get('height')}, {fmt.get('fps')} fps, {fmt.get('loudness_lufs')} LUFS")
    c.post(f"/projects/{pid}/qc").raise_for_status()
    qc = None
    for _ in range(120):
        qc = c.get(f"/projects/{pid}/delivery").json()["qc"]
        if qc and not qc["stale"]:
            break
        time.sleep(1)
    pdf = c.get(f"/projects/{pid}/qc.pdf")
    loud = next((x for x in (qc or {}).get("checks", []) if x["id"] == "loudness"), {})
    check(bool(qc) and not qc["stale"] and len(qc["checks"]) == 5 and loud.get("passed") and pdf.content.startswith(b"%PDF"),
          f"QC raporu: {', '.join(f'{x['name']} {x['summary']}' for x in (qc or {}).get('checks', []))}; PDF indirildi")
    c.post(f"/projects/{pid}/rights/script_rights", params={"filename": "hak_devri.pdf"}, content=b"%PDF-1.4 hak devri",
           headers={"content-type": "application/pdf"}).raise_for_status()
    r = c.post(f"/projects/{pid}/delivery/package").json()
    check(r["queued"] and len(r["missing"]) == 3, f"eksik hak zinciri maddesi paketi engellemedi ({', '.join(r['missing'])})")
    url = None
    for _ in range(180):
        url = c.get(f"/projects/{pid}/delivery").json()["package"]["url"]
        if url:
            break
        time.sleep(1)
    names = set()
    if url:
        z = zipfile.ZipFile(io.BytesIO(c.get(url.removeprefix("/api")).content))
        names = set(z.namelist())
    need = {"master.mov", "izleme_kopyasi.mp4", "stems/diyalog.wav", "me_mix.wav", "altyazi.srt", "film.edl", "qc_raporu.pdf",
            "hak_zinciri/yapay_zeka_beyani.csv", "hak_zinciri/script_rights/hak_devri.pdf", "BENIOKU.txt"}
    check(need <= names, f"teslim paketi {len(names)} dosya; eksik: {sorted(need - names)}")


def check_series_hierarchy(meta: dict) -> None:
    """Dizi › Sezon › Bölüm: bölüm = proje; dizi varlığı bağlanır (kopyalanmaz); yeni sürüm üretilmiş bölümü eski sürüme
    sabitler, geçiş önce etkisini gösterir; Kabul onayı bölümü kilitler ("sürüm sabit"), kilidi yönetici açar."""
    print("Y) Dizi hiyerarşisi: sezon, bölüm, dizi kütüphanesi, sürüm sabitleme ve teslim kilidi")
    q = c.post("/series", json={"title": f"duman-testi-dizi {int(time.time())}"}).json()
    season = q["seasons"][0]["id"]
    eps = [new_project("duman-testi-bolum", 100.0, lint_strictness="off") for _ in range(2)]
    for pid in eps:
        c.post(f"/projects/{pid}/episode", json={"season_id": season}).raise_for_status()
    tree = next(x for x in c.get("/series").json() if x["id"] == q["id"])
    check([e["episode_no"] for e in tree["seasons"][0]["episodes"]] == [1, 2]
          and overview(eps[1])["project"]["episode"]["season"]["number"] == 1, "iki bölüm 1. sezonda 1 ve 2 numarayla")
    kerem = c.post(f"/projects/{eps[0]}/series-library", json={"type": "character", "name": "Kerem", "link": False}).json()
    linked = [c.post(f"/projects/{pid}/script", json={"text": meta["sample_script"], "auto_render": pid == eps[0]}).json()["assets_linked"]
              for pid in eps]
    check(all("Kerem (character)" in x for x in linked), "senaryodaki Kerem iki bölümde de dizi varlığına bağlandı (kopya yok)")
    done = {s["scene_code"] for s in wait_scenes(eps[0], timeout=600)["scenes"] if s["status"] == "done"}
    before = spent(eps[0])
    c.patch(f"/assets/{kerem['asset_id']}", json={"description": "Yeni tasarım: kırmızı tulum"}).raise_for_status()
    ups = [overview(pid)["project"]["series_updates"] for pid in eps]
    planned = c.post(f"/projects/{eps[0]}/render-all").json().get("planned", {})
    check([u["name"] for u in ups[0]] == ["Kerem"] and ups[1] == [] and not any("VISUAL" in planned.get(code, []) for code in done),
          "yeni sürüm: üretilmiş 1. bölüm eski sürüme sabit, hiçbir çekim yeniden üretilmiyor; 2. bölüm son sürümü izliyor")
    impact = c.get(f"/projects/{eps[0]}/series-assets/{kerem['asset_id']}/upgrade").json()
    check(impact["affected"] and impact["expected_usd"] > 0 and spent(eps[0]) == before,
          f"geçiş önizlemesi: {len(impact['affected'])} çekim, tahmini ${impact['expected_usd']:.3f}; harcama değişmedi")
    c.post(f"/projects/{eps[0]}/series-assets/{kerem['asset_id']}/upgrade", json={"render": False}).raise_for_status()
    check(overview(eps[0])["project"]["series_updates"] == [], "bölüm yeni sürüme geçti")
    c.post(f"/projects/{eps[1]}/gates/acceptance", json={"status": "approved"}).raise_for_status()
    locked = overview(eps[1])["project"]["episode"]["locked"]
    sid = overview(eps[1])["scenes"][0]["id"]
    r = c.post(f"/scenes/{sid}/render")
    check(bool(locked) and r.status_code == 423, f"Kabul onayı bölümü kilitledi; üretim isteği {r.status_code}")
    c.post(f"/projects/{eps[1]}/unlock", json={"confirm": "kilidi aç"}).raise_for_status()
    actions = {e["action"] for e in curation(eps[1])} | {e["action"] for e in curation(eps[0])}
    check(overview(eps[1])["project"]["episode"]["locked"] is None
          and {"episode_place", "series_asset_create", "series_asset_upgrade", "client_approve", "episode_unlock"} <= actions,
          "yönetici kilidi açtı; bölüm, dizi varlığı ve kilit kararları kürasyon kaydında")


def check_sequences(pid: str) -> None:
    ov = overview(pid)
    seqs = [(q["code"], q["title"], q["shots"], len(q["tracks"])) for q in ov["sequences"]]
    check(seqs == [("SEQ01", "Dökümhane", 2, 1), ("SEQ02", "Köprü", 2, 1)],
          f"iki sekans doğru gruplandı, her birinin kendi müziği var: {seqs}")
    codes = {q["id"]: q["code"] for q in ov["sequences"]}
    check([codes.get(sc["sequence_id"]) for sc in ov["scenes"]] == ["SEQ01", "SEQ01", "SEQ02", "SEQ02"]
          and not any(t["kind"] == "music" for sc in ov["scenes"] for t in sc["tracks"]),
          "çekimler sekanslarına bağlı, çekimlerde ayrı müzik yok")


def audio_db_around(url: str | None, at: float) -> float:
    """Filmin `at` saniyesi çevresindeki (±0,3 sn) ortalama ses düzeyi, dB."""
    import subprocess
    import tempfile
    from urllib.parse import parse_qs, urlsplit

    import numpy as np

    from app import storage
    if not url:
        return float("-inf")
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        storage.download(parse_qs(urlsplit(url).query)["key"][0], f.name)      # panelin gösterdiği film dosyası
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, at - 0.3):.3f}", "-t", "0.6", "-i", f.name,
                              "-vn", "-f", "f32le", "-ac", "1", "-ar", "16000", "-"], capture_output=True).stdout
    samples = np.frombuffer(raw, dtype=np.float32)
    rms_value = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
    return 20 * np.log10(rms_value) if rms_value > 0 else float("-inf")


def pdf_subject(data: bytes) -> list[str]:
    """PDF'in Subject alanındaki çekim kodları (Pillow ASCII dışı karakterde UTF-16BE + BOM yazar)."""
    import re
    raw = re.search(rb"/Subject\s*\((.*?)(?<!\\)\)", data, re.S).group(1)
    raw = re.sub(rb"\\(.)", rb"\1", raw)
    return (raw[2:].decode("utf-16-be") if raw.startswith(b"\xfe\xff") else raw.decode("latin-1")).split(",")


def pdf_pages(data: bytes) -> int:
    import re
    return len(re.findall(rb"/Type\s*/Page\b", data))


def wait_pair(pid: str, to: str, done, timeout: float = 120) -> dict | None:
    """Süreklilik raporunda `to` çekimine giren geçiş, koşul sağlanana dek (rapor worker'da yenilenir)."""
    t0, pair = time.time(), None
    while time.time() - t0 < timeout:
        pair = next((p for p in c.get(f"/projects/{pid}/continuity").json()["pairs"] if p["to"] == to), None)
        if pair and done(pair):
            return pair
        time.sleep(1)
    return pair


def variation_count(pid: str) -> int:
    return sum(len(sc["variations"]) for sc in overview(pid)["scenes"])


def style_prefixes(pid: str, codes: list[str], latest: bool = False) -> set[str]:
    """Çekimlerin sağlayıcıya giden promptlarının, etkin stil önekiyle aynı uzunluktaki başları (hepsi aynıysa tek eleman).
    latest: yalnızca rafine havuzdaki (en son üretilen) varyasyonlar."""
    from sqlalchemy import text

    from app.db import session_scope
    with session_scope() as s:
        prompts = s.execute(text("""SELECT v.params->>'prompt' FROM variations v JOIN scenes sc ON sc.id = v.scene_id
                                    WHERE sc.project_id = :p AND sc.scene_code = ANY(:c)
                                      AND (NOT :latest OR v.refined_rank IS NOT NULL)"""),
                            {"p": pid, "c": codes, "latest": latest}).scalars().all()
    prefix = c.get(f"/projects/{pid}/style").json()["active"]["prefix"]
    return {p[:len(prefix)] for p in prompts}


def lock_asset(asset: dict, upload: str | None = None) -> dict:
    """Varlığı onay akışından geçirir: referans (verilirse gerçek yükleme, yoksa yerel taslak), durağan onay, hareket
    testi (kusurluysa yeniden), test onayı, kilit."""
    aid = asset["id"]
    if upload:
        with open(upload, "rb") as f:
            r = c.post(f"/assets/{aid}/references", params={"kind": "turnaround", "filename": os.path.basename(upload)},
                       content=f.read(), headers={"content-type": "video/mp4"})
    else:
        r = c.post(f"/assets/{aid}/references/placeholder")
    r.raise_for_status()
    c.post(f"/assets/{aid}/approve-still").raise_for_status()
    for _ in range(4):                                         # ~%12 kusur oranı: kusurlu test yenilenir
        c.post(f"/assets/{aid}/motion-test").raise_for_status()
        t0 = time.time()
        while (test := c.get(f"/assets/{aid}").json()["motion_test"])["status"] in ("queued", "running"):
            if time.time() - t0 > 120:
                raise TimeoutError(f"{asset['name']} hareket testi bitmedi")
            time.sleep(1)
        if test["status"] == "passed":
            break
    c.post(f"/assets/{aid}/approve-motion").raise_for_status()
    r = c.post(f"/assets/{aid}/lock")
    r.raise_for_status()
    return r.json()


def prepare_assets(assets: dict) -> None:
    import tempfile

    from app.engine.procedural import render_clip
    kerem = assets["Kerem"]
    r = c.post(f"/assets/{kerem['id']}/lock")
    check(r.status_code == 409 and "hareket testi onaylanmadan" in r.json()["detail"], "taslak varlık kilitlenemedi (409)")
    with tempfile.TemporaryDirectory() as tmp:                  # gerçek bir dosya yüklemesi: yönetmenin dönüş videosu
        video = render_clip(os.path.join(tmp, "kerem_donus.mp4"), 7, 640, 360, 4.0, 12,
                            camera={"shot": "medium", "movement": "orbit"}, characters=["Kerem"], label="Kerem 360")
        locked = lock_asset(kerem, upload=video)
    ref = locked["references"]["turnaround"]
    check(len(ref["frames"]) == 8 and ref["face_ref"] == ref["frames"][0] and ref["vector_dims"] == 48,
          f"dönüş videosundan {len(ref['frames'])} kare örneklendi, ilki yüz referansı, {ref['vector_dims']} boyutlu vektör")
    for name in ("Ayşe", "Terk Edilmiş Dökümhane", "Çöken Köprü"):
        lock_asset(assets[name])
    statuses = {a["name"]: a["status"] for a in c.get(f"/projects/{kerem['project_id']}/assets").json()}
    check(set(statuses.values()) == {"locked"}, f"tüm varlıklar hareket testinden geçip kilitlendi: {statuses}")


def check_defects(pid: str) -> None:
    """Kusur gerçeği (planted) ile kapının bulduğu: her bozuk klip elenmiş, havuza hiçbiri girmemiş ve havuzdaki videolar
    bağımsız bir FFmpeg ölçümünden de temiz çıkıyor."""
    import tempfile

    from sqlalchemy import select, text

    from app import storage
    from app.db import session_scope
    from app.models import Variation
    from app.quality.anomaly import detect_anomalies

    with session_scope() as s:
        rows = s.execute(text("""SELECT v.id, v.status, v.refined_rank, v.video_key, v.anomalies,
                                        v.params->'provider_meta'->>'planted_defect' AS planted, v.params->>'purge_reason' AS reason
                                 FROM variations v JOIN scenes sc ON sc.id = v.scene_id WHERE sc.project_id = :p"""), {"p": pid}).all()
    planted = [r for r in rows if r.planted]
    caught = [r for r in planted if r.status == "purged" and r.reason == "quality_gate"
              and r.planted in {a["type"] for a in r.anomalies}]
    check(len(planted) == len(caught), f"bilerek bozulan {len(planted)} klibin {len(caught)}'i kalite kapısında elendi")
    check(not [r for r in planted if r.refined_rank], "rafine havuza hiçbir bozuk klip girmedi")
    with tempfile.TemporaryDirectory() as tmp:
        dirty = 0
        pool = [r for r in rows if r.refined_rank]
        for r in pool:
            path = storage.download(r.video_key, f"{tmp}/{r.id}.mp4")
            dirty += bool(detect_anomalies(path))
    check(dirty == 0, f"havuzdaki {len(pool)} video bağımsız FFmpeg ölçümünden de temiz (siyah ya da donuk yok)")


if __name__ == "__main__":
    sys.exit(main())
