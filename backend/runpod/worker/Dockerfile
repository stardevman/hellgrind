# syntax=docker/dockerfile:1
# Hell Grind · MiniMax H3 RunPod Serverless worker'ı.
# Taban: RunPod'un resmî ComfyUI worker'ı (modelsiz -base imajı; /start.sh ComfyUI'yi açar, sonra /handler.py'yi çalıştırır).
# Modeller imajda değil, Network Volume'da (/runpod-volume/models; backend/runpod/setup_volume.sh ile indirilir).
#
#   backend/runpod/worker/push.sh docker.io/<kullanıcı>/hellgrind-h3-worker 0.39.0-1     (linux/amd64, derle + gönder)
ARG BASE=runpod/worker-comfyui:5.10.0-base
FROM ${BASE}

# Taban (5.10.0-base): CUDA 12.8.1, torch 2.11.0+cu128 (RunPod'un 570+ sürücülü Blackwell makinelerinde çalışır), ComfyUI
# 0.34.0 comfy-cli ile /comfyui'de, çalışma ortamı /opt/venv. ComfyUI kaynağı 0.39.0 sürüm arşiviyle değiştirilir: iş akışları
# (comfy-workflows/*.json) bu sürümün resmî şablonlarından dışa aktarıldı; MiniMaxH3ImageToVideo,
# MiniMaxH3ReferenceToVideo, ComfySwitchNode, ComfyMathExpression, CreateVideo, SaveVideo bu sürümde çekirdek düğümdür.
# torch yeniden kurulmaz (PyPI'nin cu13 torch'u 580 altı sürücüde açılmaz). Sürümü değiştirirseniz iş akışlarını yeniden
# dışa aktarın.
ARG COMFYUI_REF=v0.39.0
RUN set -eux; \
    wget -qO /tmp/comfyui.tgz "https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/${COMFYUI_REF}.tar.gz"; \
    mkdir /tmp/comfyui; tar -xzf /tmp/comfyui.tgz -C /tmp/comfyui --strip-components=1; rm /tmp/comfyui.tgz; \
    for d in models custom_nodes user input output; do \
      if [ -e "/comfyui/$d" ]; then rm -rf "/tmp/comfyui/$d"; mv "/comfyui/$d" "/tmp/comfyui/$d"; fi; \
    done; \
    rm -rf /comfyui; mv /tmp/comfyui /comfyui; \
    uv pip install -r /comfyui/requirements.txt "torch==2.11.0" "torchvision==0.26.0" "torchaudio==2.11.0" \
      --extra-index-url https://download.pytorch.org/whl/cu128 --index-strategy unsafe-best-match; \
    uv pip install "transformers>=4.50.3,<5" "huggingface-hub<1.0"; \
    uv pip install comfy-kitchen || echo "comfy-kitchen kurulamadı (int8 çekirdekleri ComfyUI'den gelir)"; \
    grep -rqs "MiniMaxH3ImageToVideo" /comfyui/comfy_extras /comfyui/comfy_api_nodes /comfyui/nodes.py \
      || { echo "ComfyUI ${COMFYUI_REF} MiniMax H3 düğümlerini içermiyor" >&2; exit 1; }

# H3 DiT türü torch'un CUDA etiketine uymalı (Comfy-Org): cu128 -> fp8_scaled, cu130 -> int8_convrot. Uyumsuzluk canlı
# worker'da değil derlemede durur. Uygulamadaki Ayarlar › H3 DiT türü (H3_UNET_VARIANT) bu imajla aynı olmalı.
ARG H3_UNET_VARIANT=fp8_scaled
ENV HG_UNET_VARIANT=${H3_UNET_VARIANT}
RUN python - <<'PY'
import os, re
from importlib.metadata import version
v = version("torch")
m = re.search(r"\+cu(\d+)", v)
tag = f"cu{m.group(1)}" if m else "?"
want = {"cu128": "fp8_scaled", "cu130": "int8_convrot"}.get(tag)
variant = os.environ["HG_UNET_VARIANT"]
print(f"torch {v} ({tag}); H3 DiT türü {variant}")
assert want == variant, f"torch {tag} ile H3 DiT türü {want or 'bilinmiyor'} olmalı, {variant} seçildi"
PY

# Açılış denemesi (resmî imajdaki gibi): ComfyUI'yi CPU'da başlatıp bütün düğümleri yükler; açılışı bozan bir bağımlılık
# canlı worker'da değil derlemede yakalanır. Apple Silicon'da öykünmeyle yavaştır.
RUN cd /comfyui && timeout 1200 python main.py --quick-test-for-ci --cpu

# Volume yerleşimi: models/{unet,clip,vae,loras} (+ checkpoints)
COPY extra_model_paths.yaml /comfyui/extra_model_paths.yaml
# Videoyu da döndüren, boyut/SHA-256, GPU adı ve soğuk başlatma süresini bildiren handler
COPY handler.py /handler.py

ENV REFRESH_WORKER=false \
    COMFY_LOG_LEVEL=INFO \
    HG_JOB_TIMEOUT_S=3600
