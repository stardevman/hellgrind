"""Uçtan uca duman testi — tamamen ücretsiz (DRY_RUN=1).

Kullanım: docker compose exec api python -m scripts.smoke
Kontrol edilenler:
  0. Ücretli çağrılar kod düzeyinde engelli
  1. Senaryo -> sahneler -> varyasyonlar -> Quality Gate -> Rafine Havuz -> ses -> montaj
  2. Stateless Re-render: müzik değişince yalnızca MUX, harcama değişmez
  3. Küratör seçimi: rafine #2'yi kullan -> yalnızca ses + montaj
  4. Film: tüm sahneler tek videoda birleşir
  5. Kill-Switch: 1 USD bütçeli proje limitte donar
"""
import sys
import time

import httpx

API = "http://localhost:8000/api"
c = httpx.Client(base_url=API, timeout=60)
FAILS = []
TERMINAL = {"done", "needs_review", "failed", "frozen"}


def check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


def overview(pid):
    return c.get(f"/projects/{pid}/overview").json()


def wait_all(pid: str, timeout: float = 1200) -> dict:
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
    raise TimeoutError(f"{timeout} sn içinde bitmedi")


def wait_scene(sid: str, timeout: float = 300) -> dict:
    t0 = time.time()
    time.sleep(1)
    while time.time() - t0 < timeout:
        sc = c.get(f"/scenes/{sid}").json()
        if sc["status"] in TERMINAL:
            return sc
        time.sleep(1)
    raise TimeoutError(sid)


def main() -> int:
    print("0) Güvenlik")
    meta = c.get("/meta").json()
    check(meta["dry_run"], "DRY_RUN açık")
    if not meta["dry_run"]:
        print("DRY_RUN kapalı! Para harcamamak için test durduruldu.")
        return 1
    from app.live import PaidCallBlocked, guard
    for k in ("fal_key", "elevenlabs_api_key", "anthropic_api_key"):
        try:
            guard(k)
            check(False, f"{k} engellenmedi")
        except PaidCallBlocked:
            check(True, f"{k} ile ücretli çağrı engelli")

    print("\n1) Senaryodan film sahnelerine")
    # Panelin yeni proje formuyla aynı ayar: hız bekçisi eşiği = bütçe / 2. Örnek film ~22 sanal USD'yi saniyeler içinde
    # harcar; varsayılan 20 USD / 10 dk eşiği bekçi turunun zamanlamasına göre üretimi ortasında dondururdu.
    pid = c.post("/projects", json={"title": "Smoke", "budget_limit_usd": 60,
                                    "settings": {**meta["presets"]["preview"], "max_spend_per_10min": 30}}).json()["id"]
    r = c.post(f"/projects/{pid}/script", json={"text": meta["sample_script"]}).json()
    check(r["shots"] == 4 and r["cast"] == ["Kerem", "Ayşe"], f"4 çekim, 2 karakter: {r['shots']} / {r['cast']}")
    ov = wait_all(pid)
    for s in ov["scenes"]:
        refined = [v for v in s["variations"] if v["rank"]]
        check(s["status"] == "done", f"{s['scene_code']} tamamlandı ({s['status']}{', ' + (s['last_error'] or '')[:80] if s['status'] != 'done' else ''})")
        check(1 <= len(refined) <= 3, f"{s['scene_code']} rafine havuz: {len(refined)}")
        bad = [v for v in s["variations"] if set(v["anomalies"]) & {"black", "freeze"}]
        check(all(v["status"] == "purged" for v in bad), f"{s['scene_code']} bozuk {len(bad)} klip elendi")
    s0 = ov["scenes"][0]
    spent0 = ov["project"]["spent_usd"]
    print(f"    sanal harcama: {spent0:.2f} USD")

    print("\n2) Yalnızca müzik değişiyor")
    r = c.put(f"/scenes/{s0['id']}/audio", json={"kind": "music", "source": "procedural", "params": {"mood": "sadness"}, "gain_db": -6}).json()
    check(r["stages"] == ["MUX"], f"yalnızca MUX: {r['stages']}")
    sc = wait_scene(s0["id"])
    check(sc["final_video_key"] != s0["final_url"].split("key=")[-1], "yeni final video")
    check(abs(overview(pid)["project"]["spent_usd"] - spent0) < 1e-6, "görüntü motoru çağrılmadı (harcama aynı)")

    print("\n3) Küratör seçimi")
    second = next((v for v in s0["variations"] if v["rank"] == 2), None)
    check(second is not None, "rafine havuzda 2. aday var")
    if second:
        r = c.post(f"/scenes/{s0['id']}/promote/{second['id']}").json()
        check(r["stages"] == ["LIPSYNC", "MUX"], f"yalnızca ses + montaj: {r['stages']}")
        wait_scene(s0["id"])
        check(abs(overview(pid)["project"]["spent_usd"] - spent0) < 1e-6, "harcama aynı")
    r = c.post(f"/scenes/{s0['id']}/render").json()
    check(r["stages"] == [], f"değişiklik yoksa hiçbir şey çalışmaz: {r['stages']}")

    print("\n4) Film")
    c.post(f"/projects/{pid}/assemble")
    for _ in range(120):
        tl = overview(pid)["timeline"]
        if tl:
            break
        time.sleep(2)
    check(bool(tl) and len(tl["cut"]["clips"]) == 4, "4 sahne tek filmde birleşti")
    if tl:
        print(f"    film: {tl['cut']['duration_s']:.1f} sn, panelde 'Film' bölümünden izleyebilirsin")

    print("\n5) Kill-Switch")
    pid2 = c.post("/projects", json={"title": "Smoke Kill-Switch", "budget_limit_usd": 1.0}).json()["id"]
    sid2 = c.post(f"/projects/{pid2}/scenes", json={"scene_code": "S001_SH01", "seq_order": 1}).json()["id"]
    c.post(f"/scenes/{sid2}/render")
    sc2 = wait_scene(sid2)
    p2 = c.get(f"/projects/{pid2}").json()
    check(sc2["status"] == "frozen", f"sahne donduruldu ({sc2['status']})")
    check(p2["status"] == "paused" and p2["frozen"], "proje duraklatıldı")
    check(p2["spent_usd"] <= 1.0 + 1e-6, f"limit aşılmadı: {p2['spent_usd']:.2f} / 1.00")
    check(c.post(f"/scenes/{sid2}/render").status_code == 423, "donmuş projede render reddedildi")

    print("\n" + ("TÜM KONTROLLER GEÇTİ ✅" if not FAILS else f"{len(FAILS)} KONTROL BAŞARISIZ ❌"))
    print("Panel: http://localhost:8000")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
