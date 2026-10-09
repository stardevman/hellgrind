"""Duman testi bölümü: yinelenen RunPod gönderimi yok (sahte RunPod, ağ yok).

2026-10-09 ücretli koşusunda worker süreçleri bellekten öldürülünce görevler yeniden teslim edildi ve aynı işler her ~30 sn
yeniden gönderildi (RunPod kuyruğu 44 işe çıktı). Burada aynı durum süreç içinde, sahte RunPod'la canlandırılır:
4 çekim × 2 aday gönderilir, 10 sn sonra aynı 8 iş "yeniden teslim edilir"; işler bitmez. `seconds` boyunca (varsayılan
180 sn, SMOKE_RUNPOD_SECONDS) RunPod'a giden iş sayısı sabit kalmalı ve proje sınırını (runpod_max_pending_jobs) aşmamalı.
Sonunda Kill-Switch bekleyen işleri RunPod'da iptal eder, rezervasyonlar iade edilir.

Ücretli kilit yalnızca bu süreçteki ayar nesnesinde ve sahte anahtarlarla açılır (testlerdeki `paid` fikstürü gibi); HTTP
istemcisi sahte RunPod'a bağlıdır, hiçbir istek dışarı çıkmaz. Bitince ayarlar geri yüklenir.
"""
import json
import os
import tempfile
import threading
import time

# sahte RunPod "test-runpod_api_key" anahtarını bekler
FAKE_SETTINGS = dict(dry_run=False, runpod_api_key="test-runpod_api_key", h3_platform="runpod", runpod_endpoint_id="ep123",
                     runpod_datacenter="CA-MTL-1", runpod_network_volume_id="vol1",
                     runpod_gpu_types="RTX 5090,RTX PRO 6000", runpod_min_balance_usd=10.0, h3_allow_nordic=False)


def write_workflows(flows: str) -> None:
    """Sahte RunPod'a giden en küçük iş akışları; "_hg" düğümü işin modelini taşır (sıra denetimi için)."""
    for kind, needs in (("t2v", ""), ("i2v", "{{first_frame}}"), ("r2v", "{{reference_1}}"), ("keyframe", ""),
                        ("edit_keyframe", "{{reference_1}}")):
        flow = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}", "img": needs}},
                "_hg": {"class_type": "Note", "inputs": {"kind": kind}},
                "9": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "{{filename_prefix}}"}}}
        if kind == "i2v":                              # isteğe bağlı son kare (FL2VA last_frame)
            flow["116"] = {"class_type": "LoadImage", "inputs": {"image": "{{last_frame}}"}}
            flow["6"]["inputs"]["last_frame"] = ["116", 0]
        with open(os.path.join(flows, f"{kind}.json"), "w") as f:
            json.dump(flow, f)


def run(c, check, new_project, script: str, seconds: float | None = None) -> None:
    from app.budget import killswitch, pricing
    from app.config import settings
    from app.engine import paid
    from app.engine.providers import GenRequest
    from app.live import runpod_h3, vast_box
    from tests.media_utils import make_video
    from tests.runpod_fake import FakeRunpod

    seconds = float(seconds if seconds is not None else os.environ.get("SMOKE_RUNPOD_SECONDS", 180))
    print(f"P) RunPod: yinelenen gönderim yok (sahte RunPod, 4 çekim, {seconds:.0f} sn)")
    pid = new_project("duman-testi-runpod", 5.0, lint_strictness="off")
    c.post(f"/projects/{pid}/script", json={"text": script, "auto_render": False}).raise_for_status()
    scenes = [s["id"] for s in c.get(f"/projects/{pid}/overview").json()["scenes"]]
    saved = {k: getattr(settings, k) for k in (*FAKE_SETTINGS, "h3_comfy_workflow_dir", "work_dir")}
    transport, poll = runpod_h3.TRANSPORT, runpod_h3.POLL_S
    threads: list[tuple[threading.Thread, dict]] = []
    with tempfile.TemporaryDirectory() as tmp:
        try:
            video = open(make_video(os.path.join(tmp, "v.mp4"), seconds=4.0, size="320x180", fps=24), "rb").read()
            fake = FakeRunpod(video, b"")
            fake.polls_to_finish = 10 ** 9                         # işler kuyrukta bekler, hiç bitmez
            flows = os.path.join(tmp, "comfy")
            os.makedirs(flows)
            write_workflows(flows)
            from app import runtime_config
            runtime_config.refresh_if_changed()                   # .env bir kez okunsun; sahte ayarları sonradan ezmesin
            for k, v in {**FAKE_SETTINGS, "h3_comfy_workflow_dir": flows,
                         "work_dir": os.path.join(tmp, "work")}.items():
                setattr(settings, k, v)
            runpod_h3.TRANSPORT, runpod_h3.POLL_S = fake.transport(), 0.5
            for pattern in ("rp_job:*", "rp_pending:*", "rp_cold_claim", "rp_warm"):
                for key in killswitch.r().scan_iter(vast_box._k(pattern)):
                    killswitch.r().delete(key)
            cap = int(settings.runpod_max_pending_jobs)
            estimate = pricing.estimate_video("h3", "480p", 4.0, mode="fl2va")

            def candidate(sid: str, seed: int) -> dict:
                out: dict = {}

                def go():
                    try:
                        out["result"] = paid.call(
                            pid, estimate, "h3:480p", "runpod_api_key",
                            lambda: runpod_h3.get_live_provider().generate(GenRequest(
                                prompt=f"shot {sid[:8]}", seed=seed, resolution="480p", duration_s=4.0,
                                extra={"project_id": pid, "h3_mode": "fl2va"})), scene_id=sid)
                    except BaseException as e:          # noqa: BLE001
                        out["error"] = e
                t = threading.Thread(target=go, daemon=True)
                t.start()
                threads.append((t, out))
                return out

            work = [(sid, seed) for sid in scenes for seed in (1, 2)]
            for sid, seed in work:
                candidate(sid, seed)
            time.sleep(10)
            sent = len(fake.runs)
            for sid, seed in work:                               # aynı işler yeniden teslim edildi (yeni süreçler)
                candidate(sid, seed)
            counts, t0 = [], time.monotonic()
            while time.monotonic() - t0 < seconds:
                time.sleep(min(15.0, max(1.0, seconds / 12)))
                counts.append(len(fake.runs))
            early = next((o["error"] for _, o in threads if "error" in o), None)
            if early is not None:
                print(f"    erken hata: {type(early).__name__}: {str(early)[:300]}")
            check(sent == min(len(work), cap) and all(n == sent for n in counts),
                  f"{len(work)} aday, proje sınırı {cap}: RunPod'a {sent} iş gitti; yeniden teslimden sonra {seconds:.0f} sn "
                  f"boyunca sabit kaldı ({counts})")
            check(len(runpod_h3.pending(pid)) == sent, f"projenin bekleyen RunPod işleri: {len(runpod_h3.pending(pid))}")
            killswitch.freeze(pid, "duman testi: Kill-Switch")
            for t, _ in threads:
                t.join(30)
            stopped = [o.get("error") for _, o in threads]
            check(sorted(fake.cancelled) == sorted(j["id"] for j in fake.jobs.values()) and len(fake.cancelled) == sent,
                  f"Kill-Switch RunPod'daki {len(fake.cancelled)} bekleyen işi iptal etti")
            check(all(isinstance(e, killswitch.ProjectFrozen) for e in stopped) and not any(t.is_alive() for t, _ in threads),
                  f"bekleyen {len(stopped)} görev durdu, hiçbiri yeniden göndermedi (RunPod'a toplam {len(fake.runs)} iş)")
            budget = c.get(f"/projects/{pid}/budget").json()
            check(runpod_h3.pending(pid) == {} and budget["reserved"] == 0,
                  f"bekleyen iş kaydı boş, rezervasyonlar iade edildi (ayrılan ${budget['reserved']:.4f})")
        finally:
            for k, v in saved.items():
                setattr(settings, k, v)
            runpod_h3.TRANSPORT, runpod_h3.POLL_S = transport, poll


def run_order(c, check, new_project, script: str) -> None:
    """Kare + video karışık 4 çekim: RunPod'a gönderim sırası model grubuna göre (Qwen-Image -> Qwen-Image-Edit ->
    FL2VA), en çok 2 model değişimi; videolar karelerden önce başlatılsa da kareler önce gider."""
    from app import storage
    from app.config import settings
    from app.engine.providers import GenRequest
    from app.live import runpod_h3
    from tests.media_utils import make_video
    from tests.runpod_fake import FakeRunpod
    from PIL import Image

    print("Q) RunPod: kuyruk sırası (sahte RunPod, 4 çekim, kare + video karışık)")
    pid = new_project("duman-testi-runpod-sira", 5.0, lint_strictness="off")
    saved = {k: getattr(settings, k) for k in (*FAKE_SETTINGS, "h3_comfy_workflow_dir", "work_dir")}
    transport, poll = runpod_h3.TRANSPORT, runpod_h3.POLL_S
    with tempfile.TemporaryDirectory() as tmp:
        try:
            video = open(make_video(os.path.join(tmp, "v.mp4"), seconds=4.0, size="320x180", fps=24), "rb").read()
            png = os.path.join(tmp, "ref.png")
            Image.new("RGB", (64, 64), (200, 150, 120)).save(png)
            storage.put_file(png, f"assets/smoke-{pid}/still/ref.png", "image/png")
            fake = FakeRunpod(video, open(png, "rb").read())
            fake.polls_to_finish = 3
            flows = os.path.join(tmp, "comfy")
            os.makedirs(flows)
            write_workflows(flows)
            from app import runtime_config
            runtime_config.refresh_if_changed()
            for k, v in {**FAKE_SETTINGS, "h3_comfy_workflow_dir": flows, "work_dir": os.path.join(tmp, "work")}.items():
                setattr(settings, k, v)
            runpod_h3.TRANSPORT, runpod_h3.POLL_S = fake.transport(), 0.2
            refs = [{"name": "Mila", "key": f"assets/smoke-{pid}/still/ref.png", "kind": "character"}]
            jobs = [lambda s=s: runpod_h3.get_live_provider().generate(GenRequest(
                prompt=f"shot {s}", seed=s, resolution="480p", duration_s=4.0, extra={"project_id": pid}))
                for s in (1, 2, 3, 4)]                                    # 4 çekimin videoları önce başlatılır
            jobs += [lambda s=s: runpod_h3.keyframe_image(f"board {s}", s, pid) for s in (5, 6)]
            jobs += [lambda s=s: runpod_h3.edit_keyframe_image(f"Mila {s}", s, refs, "", pid) for s in (7, 8)]
            outs = []
            for fn in jobs:
                out: dict = {}

                def go(fn=fn, out=out):
                    try:
                        out["result"] = fn()
                    except BaseException as e:      # noqa: BLE001
                        out["error"] = e
                t = threading.Thread(target=go, daemon=True)
                t.start()
                outs.append((t, out))
            for t, _ in outs:
                t.join(120)
            order = [r["input"]["workflow"]["_hg"]["inputs"]["kind"] for r in fake.runs]
            switches = sum(1 for a, b in zip(order, order[1:]) if a != b)
            failed = [o["error"] for _, o in outs if "error" in o]
            check(not failed and len(order) == 8, f"8 iş gönderildi ve bitti{'' if not failed else f' (hata: {failed[0]})'}")
            check(switches <= 2 and order.index("t2v") > max(i for i, k in enumerate(order) if k != "t2v"),
                  f"sıra {order}: {switches} model değişimi, kareler videolardan önce")
        finally:
            for k, v in saved.items():
                setattr(settings, k, v)
            runpod_h3.TRANSPORT, runpod_h3.POLL_S = transport, poll


CHAIN_SCRIPT = """INT. ATTIC WORKSHOP - NIGHT

Rain taps on the round attic window while a desk lamp glows over scattered gears.

A small round copper robot sits lifeless on the workbench.

The robot's eyes flicker on with a soft blue glow.

The robot tilts its head and looks at the window.
"""


def run_chain(c, check, new_project) -> None:
    """Anahtar kare zinciri: 4 çekimlik continuous sekansta kareler sırayla (K1 referanslardan, K2..K4 önceki kareden
    Qwen-Image-Edit ile) çizilir; video isteklerinde last_frame (sonraki çekimin karesi) vardır, sonuncuda yoktur."""
    from app.config import settings
    from app.db import session_scope
    from app.engine import storyboard
    from app.engine.iteration import _prepare
    from app.engine.providers import GenRequest
    from app.live import runpod_h3
    from app.models import Scene
    from app.quality import gate
    from sqlalchemy import select
    from tests.media_utils import make_video
    from tests.runpod_fake import FakeRunpod

    print("Z) Anahtar kare zinciri (sahte RunPod, 4 çekim, continuous)")
    pid = new_project("duman-testi-zincir", 5.0, lint_strictness="off")
    c.post(f"/projects/{pid}/script", json={"text": CHAIN_SCRIPT, "auto_render": False}).raise_for_status()
    saved = {k: getattr(settings, k) for k in (*FAKE_SETTINGS, "h3_comfy_workflow_dir", "work_dir")}
    transport, poll, frame_score = runpod_h3.TRANSPORT, runpod_h3.POLL_S, gate.score_frame
    with tempfile.TemporaryDirectory() as tmp:
        try:
            video = open(make_video(os.path.join(tmp, "v.mp4"), seconds=4.0, size="320x180", fps=24), "rb").read()
            from PIL import Image
            png = os.path.join(tmp, "k.png")
            Image.new("RGB", (64, 36), (120, 90, 60)).save(png)
            fake = FakeRunpod(video, open(png, "rb").read())
            fake.polls_to_finish = 2
            flows = os.path.join(tmp, "comfy")
            os.makedirs(flows)
            write_workflows(flows)
            from app import runtime_config
            runtime_config.refresh_if_changed()
            for k, v in {**FAKE_SETTINGS, "h3_comfy_workflow_dir": flows, "work_dir": os.path.join(tmp, "work")}.items():
                setattr(settings, k, v)
            runpod_h3.TRANSPORT, runpod_h3.POLL_S = fake.transport(), 0.1
            gate.score_frame = lambda *a, **k: gate.Score(clip=0.95, face=None, composite=0.95, anomalies=[], passed=True,
                                                          detail={})       # sıra ve istek içeriği sınanır, skor değil
            with session_scope() as s:
                shots = [(sc.scene_code, str(sc.id)) for sc in s.scalars(
                    select(Scene).where(Scene.project_id == pid).order_by(Scene.seq_order))]
            storyboard.make_board(pid, shots[-1][1])          # son kare istenir: zincir baştan sırayla çizilir
            kinds = [r["input"]["workflow"]["_hg"]["inputs"]["kind"] for r in fake.runs]
            with session_scope() as s:
                rows = [s.get(Scene, sid) for _, sid in shots]
                linked = [rows[i].board_parent == rows[i - 1].board_sha and rows[i].board_parent for i in range(1, 4)]
            check(kinds == ["keyframe", "edit_keyframe", "edit_keyframe", "edit_keyframe"] and all(linked),
                  f"kareler sırayla çizildi ({kinds}); K2..K4 bir önceki kareden (image 1)")
            prompts = [r["input"]["workflow"]["6"]["inputs"]["text"] for r in fake.runs[1:]]
            check(all("previous shot" in p for p in prompts), "zincir istemi önceki kareyi korumasını söylüyor")
            sent = []
            for code, sid in shots:
                ctx = _prepare(pid, sid, "smoke")
                runpod_h3.get_live_provider().generate(GenRequest(
                    prompt="shot", seed=1, resolution="480p", duration_s=4.0, image_url=ctx.keyframe,
                    extra={**ctx.render, **({"last_frame": ctx.last_frame} if ctx.last_frame else {})}))
                wf = fake.runs[-1]["input"]["workflow"]
                sent.append((code, "116" in wf and "last_frame" in wf["6"]["inputs"]))
            check([x for _, x in sent] == [True, True, True, False],
                  f"video isteklerinde last_frame: {sent} (son çekimde sonraki kare yok)")
        finally:
            for k, v in saved.items():
                setattr(settings, k, v)
            runpod_h3.TRANSPORT, runpod_h3.POLL_S, gate.score_frame = transport, poll, frame_score
