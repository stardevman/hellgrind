FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-quality.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Gerçek kalite skorlayıcısı (ücretsiz, yerel): CPU'luk torch, OpenCLIP, OpenCV ve sürüme sabitlenmiş model dosyaları.
# Çalışma anında ağ gerekmez. Hafif imaj için: docker compose build --build-arg QUALITY=0
ARG QUALITY=1
ENV QUALITY_MODEL_DIR=/models HF_HOME=/models/hf
RUN if [ "$QUALITY" = "1" ]; then \
      pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision \
      && pip install --no-cache-dir -r requirements-quality.txt; fi
COPY scripts/fetch_models.py scripts/
RUN if [ "$QUALITY" = "1" ]; then python scripts/fetch_models.py /models && python scripts/fetch_models.py /models --warm; fi
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

COPY . .
ENV PYTHONUNBUFFERED=1
