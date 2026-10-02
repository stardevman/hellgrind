"""Uçtan uca duman testi: tamamen ücretsiz (DRY_RUN=1), gerçek HTTP ve gerçek worker ile.

Kullanım: docker compose exec api python -m scripts.smoke
Kontroller:
  1. DRY_RUN açık ve ücretli çağrılar engelli
  2. Örnek senaryo preview ön ayarıyla 4 sahneye ayrılır, hepsi `done` olur; her sahnede rafine havuz 1-3 klip,
     siyah ve donmuş klipler elenmiştir (kusur gerçeği ile kapının bulduğu karşılaştırılır)
  3. Yalnızca müzik değişince planda yalnızca MUX vardır ve harcama değişmez
  4. Küratör seçimi yalnızca LIPSYNC ve MUX çalıştırır
  5. Değişiklik yoksa plan boştur
  6. Film 4 sahneyle birleşir
  7. 1 USD bütçeli projede sahne `frozen`, proje `paused` olur; harcama 1 USD'yi aşmaz, yeni render isteği 423 döner
Duman testi geliştirme veritabanına "duman-testi" başlıklı projeler ekler; hiçbir şey silmez.
"""
import os
import sys
import time

import httpx

API = os.environ.get("SMOKE_API", "http://localhost:8000/api")
c = httpx.Client(base_url=API, timeout=60)
FAILS: list[str] = []
TERMINAL = {"done", "needs_review", "failed", "frozen"}
CODES = ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]
BUDGET = 100.0


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


def spent(pid: str) -> float:
    return c.get(f"/projects/{pid}/budget").json()["spent"]


def new_project(title: str, budget: float) -> str:
    preset = c.get("/meta").json()["presets"]["preview"]
    # Panelin formundaki gibi: hız bekçisi bütçenin yarısı. Örnek film saniyeler içinde ~22 sanal USD harcar ve
    # varsayılan 20 USD / 10 dk sınırı filmi ortasında dondururdu.
    r = c.post("/projects", json={"title": f"{title} {int(time.time())}", "budget_limit_usd": budget,
                                  "settings": {**preset, "max_spend_per_10min": budget / 2}})
    r.raise_for_status()
    return r.json()["id"]


def main() -> int:
    t0 = time.time()

    print("1) Güvenlik")
    meta = c.get("/meta").json()
    check(meta["dry_run"] is True, "DRY_RUN açık")
    from app.live import PAID_KEYS, PaidCallBlocked, guard
    blocked = 0
    for key in PAID_KEYS:
        try:
            guard(key)
        except PaidCallBlocked:
            blocked += 1
    check(blocked == len(PAID_KEYS), f"ücretli çağrıların hepsi engelli ({blocked}/{len(PAID_KEYS)} anahtar)")

    print("2) Senaryo -> sahneler -> varyasyonlar -> Quality Gate -> Rafine Havuz -> ses -> montaj")
    pid = new_project("duman-testi", BUDGET)
    r = c.post(f"/projects/{pid}/script", json={"text": meta["sample_script"]})
    r.raise_for_status()
    check(r.json()["shots"] == 4 and r.json()["cast"] == ["Kerem", "Ayşe"], f"senaryo 4 çekime ve {r.json()['cast']} kadrosuna ayrıldı")
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

    print("7) Kill-Switch: 1 USD'lik proje")
    tiny = new_project("duman-testi-kucuk-butce", 1.0)
    c.post(f"/projects/{tiny}/script", json={"text": meta["sample_script"]}).raise_for_status()
    ov = wait_scenes(tiny, timeout=300)
    project = ov["project"]
    check("frozen" in {s["status"] for s in ov["scenes"]}, "bir sahne frozen oldu")
    check(project["status"] == "paused" and project["frozen"], f"proje paused ve dondurulmuş (durum: {project['status']})")
    check(project["spent_usd"] + project["reserved_usd"] <= 1.0, f"harcama 1 USD'yi aşmadı (${project['spent_usd']:.4f} + ayrılan ${project['reserved_usd']:.4f})")
    r = c.post(f"/scenes/{ov['scenes'][0]['id']}/render")
    check(r.status_code == 423, f"yeni render isteği 423 döndü ({r.status_code})")

    print(f"\n{time.time() - t0:.0f} sn")
    if FAILS:
        print("BAŞARISIZ KONTROLLER:\n - " + "\n - ".join(FAILS))
        return 1
    print("TÜM KONTROLLER GEÇTİ")
    return 0


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
