#!/usr/bin/env python3
"""Google Flow köprüsü: Hell Grind'in Docker'daki worker'ı ile yönetmenin Mac'indeki Google Flow oturumu arasında.

Google Flow'un API'si yoktur; Google girişi ve reCAPTCHA başsız tarayıcıda geçmez. Bu yüzden köprü Mac'te, gerçek bir
Chrome penceresini süren gflow-cli (https://github.com/ffroliva/gflow-cli, MIT) ile çalışır. Yalnızca Python standart
kütüphanesini kullanır; işleri sırayla yürütür (tek tarayıcı).

Kurulum (bir kez):
    uv tool install gflow-cli
    gflow auth login --browser chrome          # açılan Chrome'da Google hesabına gir
Çalıştırma:
    python3 tools/flow_bridge/flow_bridge.py --token <FLOW_BRIDGE_TOKEN>
Panel › Ayarlar › Google Flow: aynı belirteç, adres http://host.docker.internal:8765.

Uçlar (hepsi "Authorization: Bearer <belirteç>" ister):
    GET  /health              köprü ve gflow-cli sürümü (Flow'a dokunmaz); ?credits=1 kalan krediyi de okur
    POST /generate            multipart: mode (t2v|i2v|r2v), prompt, duration, model, aspect, code;
                              dosyalar start, end (i2v) ya da ref1..ref3 (r2v). Yanıt: video/mp4
                              (X-Flow-Credits, X-Flow-Media-Id başlıklarıyla)

Uyarı: Google Flow'u otomasyonla sürmek Google'ın kullanım şartlarına aykırıdır; hesap kapanma riski düşük ama sıfır
değildir (gflow-cli DISCLAIMER). Her üretim Flow kredisi harcar.
"""
from __future__ import annotations

import argparse
import glob
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ("t2v", "i2v", "r2v")
MODELS = ("veo-lite", "veo-fast", "veo-quality", "veo-lite-lp", "omni-flash")
DURATIONS = ("4", "6", "8", "10")
MAX_BODY = 60 * 1024 * 1024
GEN_TIMEOUT_S = 18 * 60
LOCK = threading.Lock()


class BadRequest(ValueError):
    pass


def build_command(gflow: str, mode: str, prompt: str, model: str, duration: str, aspect: str,
                  files: dict[str, str], out_dir: str) -> list[str]:
    """gflow-cli komutu (saf: test edilir). Çıktı tek klip (--count 1); i2v ve t2v -o ile, r2v --out-dir ile."""
    if mode not in MODES:
        raise BadRequest(f"mode: {', '.join(MODES)}")
    if model not in MODELS:
        raise BadRequest(f"model: {', '.join(MODELS)}")
    if duration not in DURATIONS or (duration == "10" and model != "omni-flash"):
        raise BadRequest("duration: Veo için 4, 6 ya da 8")
    if aspect not in ("16:9", "9:16"):
        raise BadRequest("aspect: 16:9 ya da 9:16")
    if not prompt.strip():
        raise BadRequest("prompt boş")
    if mode == "r2v" and model == "veo-quality":
        raise BadRequest("model: veo-quality malzeme (r2v) almaz; i2v ya da veo-fast / veo-lite")
    cmd = [gflow, "video", mode]
    if mode == "i2v":
        if "start" not in files:
            raise BadRequest("i2v başlangıç karesi (start) ister")
        cmd += ["--initial-frame", files["start"]]
        if "end" in files:
            cmd += ["--end-frame", files["end"]]
    cmd.append(prompt)
    if mode == "r2v":
        refs = [files[k] for k in sorted(files) if k.startswith("ref")]
        if not refs:
            raise BadRequest("r2v en az bir referans görseli (ref1) ister")
        for path in refs[:3]:
            cmd += ["--ref", path]
    cmd += ["--model", model]
    if model == "omni-flash":                          # Flow yalnızca Omni Flash'ta süre seçtirir; Veo kendi süresini (8 sn) verir
        cmd += ["--duration", duration]
    cmd += ["--aspect", aspect, "--count", "1", "--json"]
    cmd += ["--out-dir", out_dir] if mode == "r2v" else ["-o", os.path.join(out_dir, "clip.mp4")]
    return cmd


def parse_result(stdout: str) -> dict:
    """gflow --json çıktısının son JSON satırından kredi ve medya kimliği (bulunamazsa boş)."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        credits = next((data[k] for k in ("credits_spent", "credits", "credit_cost") if isinstance(data.get(k), int)), None)
        media = next((data[k] for k in ("media_id", "mediaId", "id") if isinstance(data.get(k), str)), None)
        return {"credits": credits, "media_id": media}
    return {}


def find_clip(out_dir: str) -> str | None:
    clips = sorted(glob.glob(os.path.join(out_dir, "**", "*.mp4"), recursive=True), key=os.path.getmtime)
    return clips[-1] if clips else None


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        raise BadRequest("multipart/form-data bekleniyor")
    fields, files = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = (os.path.basename(filename), payload)
        else:
            fields[name] = payload.decode("utf-8")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    token = ""
    gflow = "gflow"

    def _send(self, code: int, body: bytes, ctype: str = "application/json", headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode())

    def _authorized(self) -> bool:
        got = self.headers.get("Authorization", "")
        if not hmac.compare_digest(got, f"Bearer {self.token}"):
            self._json(401, {"detail": "belirteç geçersiz"})
            return False
        return True

    def do_GET(self):
        if not self.path.startswith("/health"):
            return self._json(404, {"detail": "yok"})
        if not self._authorized():
            return
        try:
            version = subprocess.run([self.gflow, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            version = ""
        out = {"ok": True, "gflow": version or None, "busy": LOCK.locked()}
        if "credits=1" in self.path and version:
            try:
                r = subprocess.run([self.gflow, "credits", "user", "--json"], capture_output=True, text=True, timeout=120)
                data = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
                out["credits"] = next((v for k, v in data.items() if "credit" in k and isinstance(v, int)), None)
            except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
                out["credits"] = None
        self._json(200, out)

    def do_POST(self):
        if self.path != "/generate":
            return self._json(404, {"detail": "yok"})
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"detail": "gövde boş ya da çok büyük"})
        tmp = tempfile.mkdtemp(prefix="flow-bridge-")
        try:
            fields, uploads = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(length))
            files = {}
            for name, (filename, data) in uploads.items():
                if name not in ("start", "end", "ref1", "ref2", "ref3"):
                    continue
                path = os.path.join(tmp, f"{name}-{filename}")
                with open(path, "wb") as f:
                    f.write(data)
                files[name] = path
            out_dir = os.path.join(tmp, "out")
            os.makedirs(out_dir)
            cmd = build_command(self.gflow, fields.get("mode", ""), fields.get("prompt", ""), fields.get("model", "veo-fast"),
                                fields.get("duration", "8"), fields.get("aspect", "16:9"), files, out_dir)
            print(f"[flow-bridge] {fields.get('code', '?')}: {' '.join(cmd[:3])} model={fields.get('model')} "
                  f"süre={fields.get('duration')} dosya={sorted(files)}", flush=True)
            with LOCK:                                         # tek tarayıcı: işler sırayla
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=GEN_TIMEOUT_S)
            if r.returncode != 0:
                tail = (r.stderr or r.stdout).strip().splitlines()[-5:]
                return self._json(502, {"detail": f"gflow çıkış kodu {r.returncode}: " + " | ".join(tail)})
            clip = find_clip(out_dir)
            if not clip:
                return self._json(502, {"detail": "gflow başarılı döndü ama mp4 bulunamadı"})
            meta = parse_result(r.stdout)
            headers = {}
            if meta.get("credits") is not None:
                headers["X-Flow-Credits"] = str(meta["credits"])
            if meta.get("media_id"):
                headers["X-Flow-Media-Id"] = meta["media_id"]
            with open(clip, "rb") as f:
                self._send(200, f.read(), "video/mp4", headers)
        except BadRequest as e:
            self._json(400, {"detail": str(e)})
        except subprocess.TimeoutExpired:
            self._json(504, {"detail": f"gflow {GEN_TIMEOUT_S // 60} dakikada bitmedi"})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def log_message(self, fmt, *args):                        # belirteç başlıkta: istek satırı yeterli
        sys.stderr.write("[flow-bridge] " + (fmt % args) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hell Grind Google Flow köprüsü (gflow-cli)")
    ap.add_argument("--token", default=os.environ.get("FLOW_BRIDGE_TOKEN", ""), help="paylaşılan belirteç (en az 16 karakter)")
    ap.add_argument("--host", default="127.0.0.1", help="dinlenen adres (Docker Desktop host.docker.internal ile ulaşır)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--gflow", default=shutil.which("gflow") or "gflow", help="gflow-cli yolu")
    a = ap.parse_args(argv)
    if len(a.token) < 16:
        ap.error("--token en az 16 karakter olmalı (ör. openssl rand -hex 24)")
    Handler.token, Handler.gflow = a.token, a.gflow
    print(f"[flow-bridge] http://{a.host}:{a.port} dinleniyor · gflow: {a.gflow}", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
