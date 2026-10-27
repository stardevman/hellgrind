"""Google Flow (Veo 3.1): yönetmenin Mac'indeki köprü üzerinden (tools/flow_bridge/flow_bridge.py).

Google Flow'un API'si yoktur; köprü gflow-cli ile gerçek bir Chrome penceresini sürer (Google girişi ve reCAPTCHA başsız
tarayıcıda geçmez). Hat köprüye tek bir çekim gönderir: istem, Flow modu (t2v / i2v / r2v), süre, model ve görseller
(başlangıç ve bitiş karesi ya da en çok 3 malzeme). Köprü klibi üretip mp4 olarak döner.

Kilit: `guard("flow_bridge_token")`. Flow kredisi abonelikten düşer; bütçeye kredi x FLOW_USD_PER_CREDIT ile yazılır.
Bir Flow üretimi kredi harcadıktan sonra indirme hata verse bile yeniden denenmez (çift harcama olmasın).
"""
from __future__ import annotations

import os

from app.config import settings
from app.live import guard

TIMEOUT_S = 20 * 60                      # Flow kuyruğu + üretim + indirme; köprü işleri sırayla yürütür
MODE_CLI = {"text": "t2v", "frames": "i2v", "ingredients": "r2v"}


class FlowBridgeError(RuntimeError):
    """Köprü ya da Flow üretimi başarısız (Türkçe ileti; gflow-cli çıkış kodu varsa ekte)."""


def generate(shot: dict, files: dict[str, str], out_path: str, model: str | None = None,
             transport=None) -> dict:
    """shot: flow_pack.FlowShot.as_dict(); files: {"start": yol, "end": yol, "ref1".."ref3": yol}. Klibi out_path'e yazar.
    Dönen: {"credits": int | None, "media_id": str | None, "model": str}."""
    token = guard("flow_bridge_token")
    import httpx                                                   # tembel: yalnızca ücretli çağrıda
    url = (settings.flow_bridge_url or "").rstrip("/")
    if not url:
        raise FlowBridgeError("Köprü adresi (FLOW_BRIDGE_URL) tanımlı değil.")
    model = model or settings.flow_model
    data = {"mode": MODE_CLI[shot["mode"]], "prompt": shot["prompt"], "duration": str(shot["seconds"]), "model": model,
            "aspect": "16:9", "code": shot["code"]}
    handles = {name: open(path, "rb") for name, path in files.items() if path}
    try:
        with httpx.Client(timeout=TIMEOUT_S, transport=transport) as client:
            r = client.post(f"{url}/generate", data=data, headers={"Authorization": f"Bearer {token}"},
                            files={name: (os.path.basename(f.name), f) for name, f in handles.items()})
    except httpx.HTTPError as e:
        raise FlowBridgeError(f"Köprüye ulaşılamadı ({type(e).__name__}): Mac'te flow_bridge.py çalışıyor mu?") from e
    finally:
        for f in handles.values():
            f.close()
    if r.status_code in (401, 403):
        raise FlowBridgeError("Köprü belirteci reddetti (FLOW_BRIDGE_TOKEN köprüdekiyle aynı değil).")
    if r.status_code != 200 or r.headers.get("content-type", "").split(";")[0] != "video/mp4":
        try:
            detail = r.json().get("detail", "")
        except ValueError:
            detail = r.text[:300]
        raise FlowBridgeError(f"Flow üretimi başarısız ({r.status_code}): {detail}")
    with open(out_path, "wb") as f:
        f.write(r.content)
    credits = r.headers.get("x-flow-credits")
    return {"credits": int(credits) if credits and credits.isdigit() else None,
            "media_id": r.headers.get("x-flow-media-id"), "model": model}
