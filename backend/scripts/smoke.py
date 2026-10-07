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
  C. Renk değişince yalnızca GRADE ve MUX çalışır; bilerek farklı renklendirilen çekim süreklilik uyarısı üretir, önerilen
     düzeltme uygulanınca uyarı kalkar (Faz 16)
  K. Kürasyon kayıtları, metrikler ve dışa aktarma (Faz 17): her panel eylemi bir kürasyon olayı üretir; metrikler
     panelin gördüğü sayılarla tutarlıdır; EDL tüm çekimleri doğru in/out değerleriyle içerir; paket zip'i açılır ve
     stem'ler çekim süresindedir; baştan üretim isteği de kaydedilir
  G. Servis geçmişi: projenin her sanal çağrısı tek satır; tutarlar harcamayla tutar, her satırın modeli var
  T. Sekans tavanı dolunca yalnızca o sekans durur, diğer sekans üretmeye devam eder (Faz 11)
  7. Küçük bütçeli (0,05 USD; MiniMax H3 bir klibi birkaç sentlik GPU süresiyle üretir) projede sahne `frozen`, proje
     `paused` olur; harcama bütçeyi aşmaz, yeni render isteği 423 döner
Duman testi geliştirme veritabanına "duman-testi" başlıklı projeler ekler; hiçbir şey silmez.
"""
import os
import sys
import time

import httpx

API = os.environ.get("SMOKE_API", "http://localhost:8000/api")
c = httpx.Client(base_url=API, timeout=60)
FAILS: list[str] = []
TERMINAL = {"done", "needs_review", "failed", "frozen", "blocked", "board_review"}
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
    preset = c.get("/meta").json()["presets"]["preview"]
    # Panelin formundaki gibi: hız bekçisi bütçenin yarısı. Yerel motor saniyeler içinde üretir; varsayılan 10 dakikalık
    # sınır, pahalı bir motor seçilmiş çekimlerde filmi ortasında dondurabilirdi.
    r = c.post("/projects", json={"title": f"{title} {int(time.time())}", "budget_limit_usd": budget,
                                  "settings": {**preset, "max_spend_per_10min": budget / 2, **cfg}})
    r.raise_for_status()
    return r.json()["id"]


def main() -> int:
    t0 = time.time()

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
        before = c.get(f"/scenes/{sid}").json()
        r = c.post(f"/scenes/{sid}/grade", json=pair["suggestion"]).json()
        fixed = wait_scene(sid, before["final_video_key"])
        pair = wait_pair(pid, "S001_SH02", lambda p: p["final"][1] == fixed["final_video_key"])
        check(bool(pair) and not pair["jump"], f"önerilen düzeltme uygulanınca uyarı kalktı (skor {pair and pair['score']})")

    check_curation_and_exports(pid)
    check_history(pid)

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

    print(f"\n{time.time() - t0:.0f} sn")
    if FAILS:
        print("BAŞARISIZ KONTROLLER:\n - " + "\n - ".join(FAILS))
        return 1
    print("TÜM KONTROLLER GEÇTİ")
    return 0


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
    check(rp["platform"] == "runpod" and rp["allowed_datacenters"] == ["CA-MTL-1", "CA-MTL-2", "CA-MTL-3"],
          f"izinli bölgeler yalnızca Kanada veri merkezleri ({', '.join(rp['allowed_datacenters'])})")
    ov = overview(pid)
    check(all(sc["h3_mode"] == "fl2va" and sc["keyframe_url"] for sc in ov["scenes"]),
          "her çekimin H3 ilk karesi var (storyboard karesinin yazısız hâli; koşul FL2VA)")
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
