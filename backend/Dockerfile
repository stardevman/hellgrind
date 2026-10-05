# Kontrol paneli (Next.js) ayrı bir aşamada statik olarak derlenir; son imaja yalnızca çıktısı girer, Node girmez.
# "frontend" bağlamını docker-compose.yml verir (additional_contexts).
FROM node:22-slim AS web
WORKDIR /web
COPY --from=frontend package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY --from=frontend . .
RUN NEXT_TELEMETRY_DISABLED=1 npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-quality.txt requirements-live.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Ücretli entegrasyonların paketleri (fal-client, langgraph, langchain-anthropic). Kurulu olmaları hiçbir şey çağırmaz:
# yalnızca app/live içinde, fonksiyon içinde ve guard() geçildikten sonra yüklenirler; DRY_RUN=1 iken hiç yüklenmezler.
# Panelden ücretli moda geçebilmek için gerekir. Bu paketler olmadan imaj için: docker compose build --build-arg LIVE=0
ARG LIVE=1
RUN if [ "$LIVE" = "1" ]; then pip install --no-cache-dir -r requirements-live.txt; fi

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
COPY --from=web /web/out /srv/panel
ENV PYTHONUNBUFFERED=1 PANEL_WEB_DIR=/srv/panel
