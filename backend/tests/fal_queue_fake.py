"""Sahte fal_client (kuyruk API'si): submit -> status -> result, cancel, upload_file. Hiçbir ağ isteği yapmaz.

Davranış uç nokta başına ayarlanır: `results[endpoint]` sırayla dönen sonuçlar (dict ya da fırlatılacak istisna),
`states[endpoint]` her durum sorgusunda dönen durumların sırası (biterse "Completed"). OpenAPI tanımları
tests/data/fal_openapi/ altında: gönderilen her gövde şemayla doğrulanır (`validate`).
"""
import json
import pathlib
import types

OPENAPI = pathlib.Path(__file__).parent / "data" / "fal_openapi"
FILES = {
    "minimax/h3-max/image-to-video": "minimax_h3-max_image-to-video.json",
    "minimax/h3-max/reference-to-video": "minimax_h3-max_reference-to-video.json",
    "bytedance/seedance-2.5/image-to-video": "bytedance_seedance-2.5_image-to-video.json",
    "bytedance/seedance-2.5/reference-to-video": "bytedance_seedance-2.5_reference-to-video.json",
    "bytedance/seedance-2.5/draft/complete": "bytedance_seedance-2.5_draft_complete.json",
    "fal-ai/qwen-image-2512": "fal-ai_qwen-image-2512.json",
    "fal-ai/qwen-image-edit-2511": "fal-ai_qwen-image-edit-2511.json",
    "fal-ai/qwen-image-edit-2511/lora": "fal-ai_qwen-image-edit-2511_lora.json",
    "fal-ai/qwen-image-edit-2511-multiple-angles": "fal-ai_qwen-image-edit-2511-multiple-angles.json",
    "fal-ai/bytedance-upscaler/upscale/video": "fal-ai_bytedance-upscaler_upscale_video.json",
    "fal-ai/kandinsky6-vsr": "fal-ai_kandinsky6-vsr.json",
    "fal-ai/seedvr/upscale/video": "fal-ai_seedvr_upscale_video.json",
    "fal-ai/flashvsr/upscale/video": "fal-ai_flashvsr_upscale_video.json",
    "blackforestlabs/flux-video-upscale": "blackforestlabs_flux-video-upscale.json",
}
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def schema(endpoint: str) -> tuple[dict, dict]:
    """(giriş şeması, bütün bileşenler)."""
    spec = json.loads((OPENAPI / FILES[endpoint]).read_text())
    comps = spec["components"]["schemas"]
    name = next(n for n in comps if n.endswith("Input"))
    return comps[name], comps


def _check(value, prop: dict, comps: dict, where: str) -> list[str]:
    if "$ref" in prop:
        prop = comps[prop["$ref"].split("/")[-1]]
    if "anyOf" in prop:
        options = [_check(value, p, comps, where) for p in prop["anyOf"]]
        return [] if any(not o for o in options) else [f"{where}: hiçbir anyOf seçeneğine uymuyor ({value!r})"]
    if "allOf" in prop:
        return [e for p in prop["allOf"] for e in _check(value, p, comps, where)]
    errors = []
    kind = prop.get("type")
    if kind == "null":
        return [] if value is None else [f"{where}: null olmalı"]
    if kind and not (isinstance(value, TYPES[kind]) and not (kind in ("integer", "number") and isinstance(value, bool))):
        return [f"{where}: tür {kind} olmalı, gelen {type(value).__name__} ({value!r})"]
    if "enum" in prop and value not in prop["enum"]:
        errors.append(f"{where}: {value!r} izinli değerlerden değil {prop['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in prop and value < prop["minimum"]:
            errors.append(f"{where}: {value} < en az {prop['minimum']}")
        if "maximum" in prop and value > prop["maximum"]:
            errors.append(f"{where}: {value} > en çok {prop['maximum']}")
    if isinstance(value, list):
        if "maxItems" in prop and len(value) > prop["maxItems"]:
            errors.append(f"{where}: {len(value)} öğe > en çok {prop['maxItems']}")
        for i, item in enumerate(value):
            errors += _check(item, prop.get("items") or {}, comps, f"{where}[{i}]")
    if isinstance(value, dict) and "properties" in prop:
        errors += validate_object(value, prop, comps, where)
    return errors


def validate_object(body: dict, sch: dict, comps: dict, where: str = "") -> list[str]:
    errors = [f"{where}{k}: zorunlu alan eksik" for k in sch.get("required") or [] if k not in body]
    for key, value in body.items():
        if key not in sch["properties"]:
            errors.append(f"{where}{key}: şemada yok")
            continue
        errors += _check(value, sch["properties"][key], comps, f"{where}{key}")
    return errors


def validate(endpoint: str, body: dict) -> list[str]:
    sch, comps = schema(endpoint)
    return validate_object(body, sch, comps)


class HTTPError(Exception):
    """fal-client'ın FalClientHTTPError'u gibi: status_code ve response (json()) taşır."""

    def __init__(self, status_code: int, detail=None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        body = {"detail": detail if detail is not None else f"HTTP {status_code}"}
        self.response = types.SimpleNamespace(status_code=status_code, json=lambda: body)


class Queued:
    pass


class InProgress:
    pass


class Completed:
    pass


STATES = {"Queued": Queued, "InProgress": InProgress, "Completed": Completed}


class FakeQueue:
    def __init__(self):
        self.submits, self.statuses, self.results, self.cancels, self.uploads = [], [], [], [], []
        self.behaviour: dict[str, list] = {}       # uç nokta -> sonuçlar (dict ya da istisna), sırayla
        self.states: dict[str, list[str]] = {}     # uç nokta -> durum adları, sırayla
        self.counter = 0
        self._result_of: dict[str, object] = {}

    def default(self, endpoint: str, args: dict) -> dict:
        if "qwen" in endpoint:
            return {"images": [{"url": f"https://fal.test/img/{self.counter}.png", "width": 1280, "height": 704}],
                    "seed": args.get("seed", 1), "prompt": args.get("prompt", ""), "has_nsfw_concepts": [False],
                    "timings": {}}
        out = {"video": {"url": f"https://fal.test/video/{self.counter}.mp4", "content_type": "video/mp4"},
               "seed": args.get("seed", 42)}
        if endpoint.startswith("minimax/"):
            out.update(expanded_prompt=None, timings={"inference": 81.5})
        else:
            out.update(draft_id=None)
        return out

    def module(self):
        fake = self

        class Handle:
            def __init__(self, request_id):
                self.request_id = request_id

        class SyncClient:
            def __init__(self, key=None, default_timeout=120.0):
                pass

            def submit(self, application, arguments, **kw):
                fake.counter += 1
                rid = f"fal-req-{fake.counter}"
                fake.submits.append((application, arguments))
                queue = fake.behaviour.get(application) or []
                fake._result_of[rid] = queue.pop(0) if queue else fake.default(application, arguments)
                return Handle(rid)

            def status(self, application, request_id, with_logs=False):
                fake.statuses.append((application, request_id))
                states = fake.states.get(application) or []
                name = states.pop(0) if states else "Completed"
                if isinstance(name, BaseException):
                    raise name
                return STATES[name]()

            def result(self, application, request_id):
                fake.results.append((application, request_id))
                value = fake._result_of.get(request_id)
                if value is None:                          # sürdürülen iş (bu sahte istemcide gönderilmedi)
                    value = fake.default(application, {})
                if isinstance(value, BaseException):
                    raise value
                return value

            def cancel(self, application, request_id):
                fake.cancels.append((application, request_id))

            def upload_file(self, path):
                fake.uploads.append(str(path))
                return f"https://v3b.fal.media/files/test/{len(fake.uploads)}"

        mod = types.ModuleType("fal_client")
        mod.SyncClient = SyncClient
        return mod

    def endpoints(self) -> list[str]:
        return [a for a, _ in self.submits]
