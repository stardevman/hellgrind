#!/bin/bash
# Hell Grind · RunPod worker imajını derleyip bir kayda (Docker Hub ya da GHCR) gönderir.
#
#   backend/runpod/worker/push.sh docker.io/<kullanıcı>/hellgrind-h3-worker            # etiket: 0.39.0-1
#   backend/runpod/worker/push.sh ghcr.io/<kullanıcı>/hellgrind-h3-worker 0.39.0-2
#
# Etiket: <ComfyUI sürümü>-<bizim düzeltme numaramız>. handler.py ya da Dockerfile değişince numarayı artırın; "latest"
# kullanmayın (RunPod worker'ları imajı önbelleğe alır, aynı etiketle yeni imaj yüklenmeyebilir). Önce `docker login`.
# Apple Silicon'da linux/amd64 öykünmeyle derlenir (yavaştır ama çalışır); RunPod GPU'ları amd64'tür.
set -euo pipefail
IMAGE=${1:?kullanım: push.sh <kayıt>/<kullanıcı>/hellgrind-h3-worker [etiket]}
TAG=${2:-0.39.0-1}
HERE=$(cd "$(dirname "$0")" && pwd)
docker buildx build --platform linux/amd64 -t "$IMAGE:$TAG" --push "$HERE"
echo "Gönderildi. RunPod Serverless uç noktasının Container Image alanına: $IMAGE:$TAG"
