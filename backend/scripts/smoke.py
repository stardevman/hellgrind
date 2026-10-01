"""Kademe 1 uçtan uca duman testi (sıfır maliyet, DRY_RUN=1).

Kullanım: docker compose exec api python -m scripts.smoke
Kontrol edilenler:
  1. Sahne üretimi: test turu -> tam tur -> Quality Gate -> Rafine Havuz (en fazla 3)
  2. Ses + muksing ile final video
  3. Stateless Re-render: yalnızca müzik değişince sadece MUX çalışır, video harcaması artmaz
  4. Kill-Switch: küçük bütçeli proje limitte donar, harcama limiti aşmaz
"""
import sys
import time

import httpx

API = "http://localhost:8000/api"
c = httpx.Client(base_url=API, timeout=30)
FAILS = []


def check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


def wait_scene(scene_id: str, done: set[str], timeout: float = 300) -> dict:
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        sc = c.get(f"/scenes/{scene_id}").json()
        if sc["status"] != last:
            print(f"    durum: {sc['status']}")
            last = sc["status"]
        if sc["status"] in done:
            return sc
        time.sleep(1)
    raise TimeoutError(f"sahne {scene_id} {timeout} sn'de bitmedi (son durum: {last})")


def new_project(title: str, budget: float, **settings) -> str:
    r = c.post("/projects", json={"title": title, "budget_limit_usd": budget, "settings": settings})
    r.raise_for_status()
    return r.json()["id"]


def new_scene(pid: str, code: str, **kw) -> str:
    body = {"scene_code": code, "seq_order": int(code[1:4]) * 100 + int(code[-2:]),
            "description": "Kerem çöken köprünün kenarında dengesini kaybeder.",
            "emotion": "fear", "prompt_spec": {"prompt": "[CHAR:Kerem] stumbling on a collapsing bridge"}, **kw}
    r = c.post(f"/projects/{pid}/scenes", json=body)
    r.raise_for_status()
    return r.json()["id"]


def main() -> int:
    print("0) Sağlık kontrolü")
    h = httpx.get("http://localhost:8000/healthz").json()
    check(h["ok"] and h["dry_run"], "API ayakta ve DRY_RUN açık")
    if not h["dry_run"]:
        print("DRY_RUN kapalı! Para harcamamak için testi durduruyorum.")
        return 1

    print("\n1) Üretim: test turu -> tam tur -> Rafine Havuz")
    pid = new_project("Smoke - ana", 25, full_n=20)
    sid = new_scene(pid, "S001_SH01")
    r = c.put(f"/scenes/{sid}/audio", json={"kind": "dialogue", "text": "Dayan! Bırakma elimi!", "emotion": "fear",
                                             "start_offset_s": 0.5}).json()
    check(r["stages"] == ["VISUAL", "LIPSYNC", "MUX"], f"ilk render tüm aşamaları planladı: {r['stages']}")
    sc = wait_scene(sid, {"done", "needs_review", "failed", "frozen"})
    check(sc["status"] == "done", f"sahne tamamlandı (durum: {sc['status']}, hata: {sc.get('last_error')})")
    vs = c.get(f"/scenes/{sid}/variations").json()
    refined = [v for v in vs if v["refined_rank"]]
    purged = [v for v in vs if v["status"] == "purged"]
    print(f"    varyasyon: {len(vs)} toplam, {len(refined)} rafine, {len(purged)} imha")
    check(1 <= len(refined) <= 3, "Rafine Havuz 1-3 varyasyon içeriyor")
    check(all(v["video_key"] is None for v in purged), "imha edilenlerin dosyası silindi")
    check(any(a["type"] in ("black", "freeze") for v in vs for a in v["anomalies"]),
          "anomali tespiti en az bir bozuk videoyu yakaladı")
    spent_before = c.get(f"/projects/{pid}").json()["spent_usd"]
    final_before = sc["final_video_key"]
    print(f"    harcanan (sahte): {spent_before:.2f} USD")

    print("\n2) Stateless Re-render: yalnızca müzik değişiyor")
    r = c.put(f"/scenes/{sid}/audio", json={"kind": "music", "source": "sample", "gain_db": -6}).json()
    check(r["stages"] == ["MUX"], f"yalnızca MUX planlandı: {r['stages']}")
    sc = wait_scene(sid, {"done", "failed", "frozen"})
    spent_after = c.get(f"/projects/{pid}").json()["spent_usd"]
    check(sc["final_video_key"] != final_before, "yeni final video üretildi")
    check(abs(spent_after - spent_before) < 1e-6, "video motoru çağrılmadı (harcama değişmedi)")
    print(f"    final video URL: {c.get(f'/scenes/{sid}/final-url').json()['url'][:90]}...")

    print("\n3) Aynı girdiyle tekrar render: hiçbir şey çalışmamalı")
    r = c.post(f"/scenes/{sid}/render").json()
    check(r["stages"] == [], f"plan boş: {r['stages']}")

    print("\n4) Kill-Switch: 1 USD bütçeli proje")
    pid2 = new_project("Smoke - killswitch", 1.0)
    sid2 = new_scene(pid2, "S002_SH01")
    c.post(f"/scenes/{sid2}/render")
    sc2 = wait_scene(sid2, {"frozen", "done", "needs_review", "failed"})
    p2 = c.get(f"/projects/{pid2}").json()
    check(sc2["status"] == "frozen", f"sahne donduruldu (durum: {sc2['status']})")
    check(p2["status"] == "paused" and p2["frozen"], "proje duraklatıldı")
    check(p2["spent_usd"] <= 1.0 + 1e-6, f"harcama limiti aşmadı: {p2['spent_usd']:.2f} / 1.00 USD")
    r = c.post(f"/scenes/{sid2}/render")
    check(r.status_code == 423, "donmuş projede yeni render reddedildi (423)")

    print("\n" + ("TÜM KONTROLLER GEÇTİ ✅" if not FAILS else f"{len(FAILS)} KONTROL BAŞARISIZ ❌"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
