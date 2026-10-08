# Hell Grind · MiniMax H3 RunPod Serverless worker'ı.
# Taban: RunPod'un resmî ComfyUI worker'ı (modelsiz -base imajı; /start.sh ComfyUI'yi açar, sonra /handler.py'yi çalıştırır).
# Modeller imajda değil, Network Volume'da (/runpod-volume/models; backend/runpod/setup_volume.sh ile indirilir).
#
#   docker build --platform linux/amd64 -t <kullanıcı>/hellgrind-h3-worker:1 backend/runpod/worker
#   docker push <kullanıcı>/hellgrind-h3-worker:1
ARG BASE=runpod/worker-comfyui:5.11.0-base
FROM ${BASE}

# ComfyUI 0.39.0'a sabitlenir: iş akışları (comfy-workflows/*.json) bu sürümün resmî şablonlarından dışa aktarıldı;
# MiniMaxH3ImageToVideo, MiniMaxH3ReferenceToVideo, ComfySwitchNode, ComfyMathExpression, CreateVideo, SaveVideo bu sürümde
# çekirdek düğümdür (özel düğüm gerekmez). Sürümü değiştirirseniz iş akışlarını yeniden dışa aktarın.
ARG COMFYUI_REF=v0.39.0
RUN set -eux; \
    cd /comfyui; \
    if [ ! -d .git ]; then echo "/comfyui bir git deposu değil: taban imajın yerleşimi değişmiş" >&2; exit 1; fi; \
    git fetch --depth 1 origin "${COMFYUI_REF}"; \
    git checkout -f FETCH_HEAD; \
    python -m pip install --no-cache-dir -r requirements.txt; \
    python -m pip install --no-cache-dir comfy-kitchen || echo "comfy-kitchen kurulamadı (int8 çekirdekleri ComfyUI'den gelir)"

# Volume yerleşimi: models/{unet,clip,vae,loras} (+ checkpoints)
COPY extra_model_paths.yaml /comfyui/extra_model_paths.yaml
# Videoyu da döndüren, boyut/SHA-256, GPU adı ve soğuk başlatma süresini bildiren handler
COPY handler.py /handler.py

ENV REFRESH_WORKER=false \
    COMFY_LOG_LEVEL=INFO \
    HG_JOB_TIMEOUT_S=3600
