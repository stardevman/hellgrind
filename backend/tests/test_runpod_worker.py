"""RunPod worker handler'ı (backend/runpod/worker/handler.py): sahte ComfyUI ile. Video da döner, boyut/SHA-256 ve bedel
bilgisi bildirir, büyük çıktıyı S3 olmadan sessizce kaybetmez. Ağ yok."""
import base64
import hashlib
import importlib.util
import json
import pathlib

import pytest

WORKER = pathlib.Path(__file__).resolve().parents[1] / "runpod" / "worker"


@pytest.fixture
def worker(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("hg_worker_handler", WORKER / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "output"
    (out / "hellgrind").mkdir(parents=True)
    video = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40
    (out / "hellgrind" / "i2v_00001_.mp4").write_bytes(video)
    monkeypatch.setattr(mod, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(mod, "POLL_S", 0)
    monkeypatch.setattr(mod, "gpu_name", lambda: "NVIDIA GeForce RTX 5090")
    calls = []

    def http(method, path, body=None, headers=None, timeout=30.0):
        calls.append((method, path, body))
        if path == "/system_stats":
            return 200, b"{}"
        if path == "/upload/image":
            return 200, b'{"name": "x"}'
        if path == "/prompt":
            prompt = json.loads(body)["prompt"]
            if prompt.get("bad"):
                return 400, json.dumps({"node_errors": {"7": {"errors": [{"message": "Value not in list"}]}}}).encode()
            return 200, b'{"prompt_id": "p1"}'
        if path == "/history/p1":
            entry = {"status": {"status_str": "success", "completed": True,
                                "messages": [["execution_start", {"timestamp": 1000}], ["execution_success", {"timestamp": 81000}]]},
                     "outputs": {"9": {"images": [{"filename": "i2v_00001_.mp4", "subfolder": "hellgrind", "type": "output"}],
                                       "animated": [True]}, "3": {"gifs": [{"filename": "prev.png", "type": "temp"}]}}}
            return 200, json.dumps({"p1": entry}).encode()
        return 404, b""

    monkeypatch.setattr(mod, "http", http)
    mod.calls, mod.video = calls, video
    return mod


def job(**inp):
    return {"id": "rp-1", "input": {"workflow": {"6": {"class_type": "X", "inputs": {}}}, "images": [
        {"name": "hg_a.jpg", "image": base64.b64encode(b"jpeg").decode()}], "hellgrind": {"kind": "video"}, **inp}}


def test_a_video_is_returned_with_size_hash_gpu_and_cold_start(worker):
    out = worker.handler(job())
    (f,) = out["files"]
    assert f["filename"] == "i2v_00001_.mp4" and f["type"] == "base64"
    assert base64.b64decode(f["data"]) == worker.video and f["size"] == len(worker.video)
    assert f["sha256"] == hashlib.sha256(worker.video).hexdigest()
    assert out["gpu"] == "NVIDIA GeForce RTX 5090" and out["timing"]["comfy_s"] == 80.0
    assert out["timing"]["cold_start_s"] >= 0 and worker.handler(job())["timing"]["cold_start_s"] == 0.0   # yalnızca ilk iş
    assert [c[1] for c in worker.calls[:3]] == ["/system_stats", "/upload/image", "/prompt"]


def test_an_output_too_large_for_the_response_fails_loudly_without_s3(worker, monkeypatch):
    monkeypatch.delenv("BUCKET_ENDPOINT_URL", raising=False)
    out = worker.handler(job(hellgrind={"kind": "video", "inline_limit": 100}))
    assert "S3 tanımlı değil" in out["error"] and "files" not in out


def test_large_outputs_go_to_s3_as_a_signed_url(worker, monkeypatch):
    monkeypatch.setenv("BUCKET_ENDPOINT_URL", "https://r2.test")
    monkeypatch.setattr(worker, "upload_s3", lambda cfg, job_id, name, data: f"https://r2.test/{job_id}/{name}?sig=1")
    (f,) = worker.handler(job(hellgrind={"kind": "video", "inline_limit": 100}))["files"]
    assert f["type"] == "url" and f["data"].startswith("https://r2.test/rp-1/") and f["sha256"]


def test_an_int8_convrot_unet_is_refused_on_a_cu128_worker(worker, monkeypatch):
    monkeypatch.setattr(worker, "torch_version", lambda: "2.11.0+cu128")
    unet = {"7": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors"}}}
    out = worker.handler(job(workflow=unet))
    assert "cu130 torch ister" in out["error"] and not any(c[1] == "/prompt" for c in worker.calls)
    fp8 = {"7": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_pruned_fp8_scaled.safetensors"}}}
    assert worker.handler(job(workflow=fp8))["torch"] == "2.11.0+cu128"
    monkeypatch.setattr(worker, "torch_version", lambda: "2.14.1+cu130")
    assert "cu130" not in worker.handler(job(workflow=unet)).get("error", "")                # cu130 worker'da geçer


def test_comfy_rejections_and_missing_outputs_are_errors(worker):
    assert "Value not in list" in worker.handler(job(workflow={"bad": {"class_type": "X", "inputs": {}}}))["error"]
    assert "görsel çıktısı vermedi" in worker.handler(job(hellgrind={"kind": "keyframe"}))["error"]
    assert "workflow" in worker.handler({"input": {}})["error"]
