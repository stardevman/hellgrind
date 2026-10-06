#!/bin/bash
# Hell Grind · MiniMax H3 makinesi kurulumu (vast.ai, vastai/base-image).
#
# Kurar: ComfyUI (güncel; MiniMax H3 yerel düğümleri), PyTorch (CUDA 12.8, RTX 5090 için), MiniMax H3 modelleri
# (Comfy-Org, budanmış int8_convrot FL2VA + Ref2VA, metin kodlayıcı, VAE'ler, turbo LoRA'lar) ve storyboard ilk karesi için
# Apache 2.0 lisanslı bir görsel modeli (Qwen-Image-2512 ya da FLUX.1 [schnell]). ComfyUI yalnızca 127.0.0.1'de dinler;
# dışarıya vast.ai Instance Portal'ın kimlik doğrulamalı vekiliyle açılır (PORTAL_CONFIG "localhost:8188:18188:/:ComfyUI",
# parola WEB_PASSWORD). Her açılışta güvenle yeniden çalışır: inmiş dosyalar atlanır.
#
# Hell Grind yedek makine kiralarken bu betiği isteğe gömer (gzip + base64, onstart). Elle: makinede
#   bash setup.sh            (ortam: HG_IMAGE_MODEL, HG_H3_TEXT_ENCODER, HG_COMFY_PORT, HF_TOKEN; günlük /var/log/hellgrind-setup.log)
set -uo pipefail

LOG=/var/log/hellgrind-setup.log
exec > >(tee -a "$LOG") 2>&1
WORKSPACE=${WORKSPACE:-/workspace}
COMFY="$WORKSPACE/ComfyUI"
VENV=${HG_VENV:-/venv/main}
IMAGE_MODEL=${HG_IMAGE_MODEL:-qwen-image-2512}            # qwen-image-2512 | flux-schnell | none
TEXT_ENCODER=${HG_H3_TEXT_ENCODER:-qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors}   # NVFP4: RTX 50 serisi (CC 10+)
PORT=${HG_COMFY_PORT:-18188}
HF=https://huggingface.co
step() { echo "[hellgrind $(date +%H:%M:%S)] $*"; }

step "kurulum başladı (görsel modeli: $IMAGE_MODEL)"
touch "$WORKSPACE/.hellgrind-setup-running" 2>/dev/null || true

# 1) ComfyUI ve Python ortamı
if [ -d "$COMFY/.git" ]; then git -C "$COMFY" pull --ff-only || step "ComfyUI güncellenemedi; mevcut sürüm kullanılıyor"
else git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFY" || { step "HATA: ComfyUI indirilemedi"; exit 1; }; fi
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
PY="$VENV/bin/python"
"$PY" -m pip install -q --upgrade pip
"$PY" -c "import torch, sys; v = tuple(map(int, (torch.version.cuda or '0.0').split('.')[:2])); sys.exit(0 if v >= (12, 8) else 1)" 2>/dev/null \
  || "$PY" -m pip install -q torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
"$PY" -m pip install -q -r "$COMFY/requirements.txt"
"$PY" -m pip install -q comfy-kitchen || step "comfy-kitchen kurulamadı (int8 çekirdekleri ComfyUI'nin kendisinden gelir)"

# 2) Modeller (Hugging Face; kesilen indirme kaldığı yerden sürer)
fetch() {  # depo  depodaki-yol  hedef-klasör
  local repo=$1 path=$2 dir=$3 name; name=$(basename "$path")
  mkdir -p "$dir"
  if [ -s "$dir/$name" ]; then step "var: $name"; return 0; fi
  step "indiriliyor: $repo/$path"
  curl -fL --retry 8 --retry-delay 15 -C - ${HF_TOKEN:+-H "Authorization: Bearer $HF_TOKEN"} \
    -o "$dir/$name.part" "$HF/$repo/resolve/main/$path" && mv "$dir/$name.part" "$dir/$name" \
    || { step "HATA: indirilemedi: $path"; return 1; }
}
M="$COMFY/models"
H3=Comfy-Org/MiniMax-H3
ok=0
fetch $H3 diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors "$M/diffusion_models" || ok=1
fetch $H3 diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors "$M/diffusion_models" || ok=1
fetch $H3 "text_encoders/$TEXT_ENCODER" "$M/text_encoders" || ok=1
fetch $H3 vae/minimax_h3_video_vae_int8_convrot.safetensors "$M/vae" || ok=1
fetch $H3 vae/minimax_h3_audio_vae_fp32.safetensors "$M/vae" || ok=1
fetch $H3 loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors "$M/loras" || ok=1
fetch $H3 loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors "$M/loras" || ok=1
case "$IMAGE_MODEL" in
  qwen-image-2512)          # Apache 2.0 (Qwen/Qwen-Image-2512)
    fetch Comfy-Org/Qwen-Image_ComfyUI split_files/diffusion_models/qwen_image_2512_fp8_e4m3fn.safetensors "$M/diffusion_models" || ok=1
    fetch Comfy-Org/Qwen-Image_ComfyUI split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors "$M/text_encoders" || ok=1
    fetch Comfy-Org/Qwen-Image_ComfyUI split_files/vae/qwen_image_vae.safetensors "$M/vae" || ok=1 ;;
  flux-schnell)             # Apache 2.0 (black-forest-labs/FLUX.1-schnell); [dev] ticari olmayan lisanslıdır, kurulmaz
    fetch Comfy-Org/flux1-schnell flux1-schnell-fp8.safetensors "$M/checkpoints" || ok=1 ;;
  none) step "görsel modeli kurulmadı (storyboard karesi fal.ai'den ya da yüklenerek)" ;;
  *) step "HATA: bilinmeyen görsel modeli: $IMAGE_MODEL"; ok=1 ;;
esac

# 3) ComfyUI servisi: yalnızca 127.0.0.1; portal (Caddy) kimlik doğrulayıp 8188'den açar
cat > /etc/supervisor/conf.d/comfyui.conf <<CONF
[program:comfyui]
command=$PY $COMFY/main.py --listen 127.0.0.1 --port $PORT --disable-auto-launch
directory=$COMFY
autostart=true
autorestart=true
stopasgroup=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
redirect_stderr=true
CONF
for _ in $(seq 1 60); do supervisorctl status >/dev/null 2>&1 && break; sleep 5; done
supervisorctl reread && supervisorctl update || step "HATA: supervisor ComfyUI'yi başlatamadı"
rm -f "$WORKSPACE/.hellgrind-setup-running" 2>/dev/null || true
if [ $ok -eq 0 ]; then step "kurulum bitti: ComfyUI 127.0.0.1:$PORT"; else step "kurulum eksik bitti: yukarıdaki HATA satırlarına bakın"; fi
exit $ok
