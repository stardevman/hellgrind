#!/bin/bash
# Hell Grind · MiniMax H3 makinesi kurulumu (vast.ai, vastai/base-image).
#
# Kurar: ComfyUI (güncel; MiniMax H3 yerel düğümleri), PyTorch (CUDA 12.8, RTX 5090 için), MiniMax H3 modelleri
# (Comfy-Org, budanmış int8_convrot FL2VA + Ref2VA, metin kodlayıcı, VAE'ler, turbo LoRA'lar) ve storyboard ilk karesi için
# Apache 2.0 lisanslı bir görsel modeli (Qwen-Image-2512 ya da FLUX.1 [schnell]). ComfyUI yalnızca 127.0.0.1'de dinler;
# dışarıya vast.ai Instance Portal'ın kimlik doğrulamalı vekiliyle açılır (PORTAL_CONFIG "localhost:8188:18188:/:ComfyUI",
# parola WEB_PASSWORD / OPEN_BUTTON_TOKEN). Her açılışta güvenle yeniden çalışır: inmiş dosyalar atlanır.
#
# Kaynaklar: ilk gerçek denemede yalnızca H3 kapsayıcıda ~94 GB RAM kullandı (sayfa önbelleği dahil); Qwen-Image ~30 GB
# ekler. Önerilen: RAM >= 128 GB (en az 96), disk >= 200 GB, indirme >= 1 Gbps (modeller ~105 GB).
#
# Günlük: /var/log/hellgrind/setup.log (eski yol /var/log/hellgrind-setup.log bağlantıdır). Her satır saat ve başlangıçtan
# geçen süreyle başlar; her aşamanın ve her indirmenin süresi, sonda toplam kurulum süresi ve portal sertifikasının SHA-256
# parmak izi yazılır (Hell Grind'in sabitlediğiyle karşılaştırın). Günlük, portalın kimlik doğrulamalı 8189 portundan da
# okunur (PORTAL_CONFIG "localhost:8189:18189:/:Hellgrind kurulum"): panel kurulumu buradan izler.
#
# Hell Grind yeni makine kiralarken bu betiği isteğe gömer (gzip + base64, onstart). Elle: makinede
#   bash setup.sh            (ortam: HG_IMAGE_MODEL, HG_H3_TEXT_ENCODER, HG_COMFY_PORT, HG_LOG_PORT, HF_TOKEN)
set -uo pipefail

START=$(date +%s)
LOGDIR=/var/log/hellgrind
mkdir -p "$LOGDIR"
LOG="$LOGDIR/setup.log"
ln -sf "$LOG" /var/log/hellgrind-setup.log 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1
WORKSPACE=${WORKSPACE:-/workspace}
COMFY="$WORKSPACE/ComfyUI"
VENV=${HG_VENV:-/venv/main}
IMAGE_MODEL=${HG_IMAGE_MODEL:-qwen-image-2512}            # qwen-image-2512 | flux-schnell | none
TEXT_ENCODER=${HG_H3_TEXT_ENCODER:-qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors}   # NVFP4: RTX 50 serisi (CC 10+)
PORT=${HG_COMFY_PORT:-18188}
LOG_PORT=${HG_LOG_PORT:-18189}
HF=https://huggingface.co
DL_BYTES=0
PHASE=""
PHASE_T=0

since() { local s=$(( $(date +%s) - $1 )); printf '%d dk %02d sn' $((s / 60)) $((s % 60)); }
step() { echo "[hellgrind $(date +%H:%M:%S) +$(since "$START")] $*"; }
phase_end() { [ -n "$PHASE" ] && step "== $PHASE bitti ($(( $(date +%s) - PHASE_T )) sn) =="; PHASE=""; }
phase() { phase_end; PHASE=$1; PHASE_T=$(date +%s); step "== $1 =="; }

# Kurulum günlüğü sunucusu (yalnızca 127.0.0.1; portal kimlik doğrulayıp açar)
if ! pgrep -f "http.server $LOG_PORT" >/dev/null 2>&1; then
  (cd "$LOGDIR" && nohup python3 -m http.server "$LOG_PORT" --bind 127.0.0.1 >/dev/null 2>&1 &)
fi

ram_limit() {  # kapsayıcının RAM sınırı (cgroup); sınır yoksa sunucunun toplamı (vast.ai kapsayıcıya sınır koymayabilir)
  local max; max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null)
  if [[ "$max" =~ ^[0-9]+$ ]] && [ "$max" -lt 4611686018427387904 ]; then echo "$(( max / 1073741824 )) GB (kapsayıcı sınırı)"
  else echo "$(free -g 2>/dev/null | awk '/^Mem:/{print $2}') GB (sunucunun toplamı; kapsayıcı sınırı okunamadı, kiralanan pay teklifte yazar)"; fi
}
free_disk() {  # /workspace henüz yoksa üst klasör ya da kök
  local d=$WORKSPACE; while [ ! -d "$d" ] && [ "$d" != "/" ]; do d=$(dirname "$d"); done
  df -BG --output=avail "$d" 2>/dev/null | tail -1 | tr -d ' '
}
step "kurulum başladı (görsel modeli: $IMAGE_MODEL)"
step "makine: GPU $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1 || echo '?'); RAM $(ram_limit); boş disk $(free_disk)"
touch "$WORKSPACE/.hellgrind-setup-running" 2>/dev/null || true

# 1) ComfyUI ve Python ortamı
phase "1/4 ComfyUI ve Python ortamı"
if [ -d "$COMFY/.git" ]; then git -C "$COMFY" pull --ff-only || step "ComfyUI güncellenemedi; mevcut sürüm kullanılıyor"
else git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFY" || { step "HATA: ComfyUI indirilemedi"; step "KURULUM_BITTI durum=hata sure=$(( $(date +%s) - START ))"; exit 1; }; fi
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
PY="$VENV/bin/python"
"$PY" -m pip install -q --upgrade pip
"$PY" -c "import torch, sys; v = tuple(map(int, (torch.version.cuda or '0.0').split('.')[:2])); sys.exit(0 if v >= (12, 8) else 1)" 2>/dev/null \
  || "$PY" -m pip install -q torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
"$PY" -m pip install -q -r "$COMFY/requirements.txt"
"$PY" -m pip install -q comfy-kitchen || step "comfy-kitchen kurulamadı (int8 çekirdekleri ComfyUI'nin kendisinden gelir)"

# 2) Modeller (Hugging Face; kesilen indirme kaldığı yerden sürer)
fetch() {  # depo  depodaki-yol  hedef-klasör
  local repo=$1 path=$2 dir=$3 name t0 secs size; name=$(basename "$path")
  mkdir -p "$dir"
  if [ -s "$dir/$name" ]; then step "var: $name"; return 0; fi
  step "indiriliyor: $repo/$path"
  t0=$(date +%s)
  curl -fsSL --retry 8 --retry-delay 15 -C - ${HF_TOKEN:+-H "Authorization: Bearer $HF_TOKEN"} \
    -o "$dir/$name.part" "$HF/$repo/resolve/main/$path" && mv "$dir/$name.part" "$dir/$name" \
    || { step "HATA: indirilemedi: $path"; return 1; }
  size=$(stat -c %s "$dir/$name" 2>/dev/null || echo 0)
  DL_BYTES=$(( DL_BYTES + size ))
  secs=$(( $(date +%s) - t0 )); [ "$secs" -gt 0 ] || secs=1
  step "indi: $name ($(( size / 1048576 )) MB, $secs sn, $(( size / 1048576 / secs )) MB/sn)"
}
M="$COMFY/models"
H3=Comfy-Org/MiniMax-H3
ok=0
phase "2/4 MiniMax H3 modelleri"
fetch $H3 diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors "$M/diffusion_models" || ok=1
fetch $H3 diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors "$M/diffusion_models" || ok=1
fetch $H3 "text_encoders/$TEXT_ENCODER" "$M/text_encoders" || ok=1
fetch $H3 vae/minimax_h3_video_vae_fp16.safetensors "$M/vae" || ok=1     # iş akışları fp16 VAE bekler
fetch $H3 vae/minimax_h3_audio_vae_fp32.safetensors "$M/vae" || ok=1
fetch $H3 loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors "$M/loras" || ok=1
fetch $H3 loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors "$M/loras" || ok=1
phase "3/4 Görsel modeli ($IMAGE_MODEL)"
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
phase "4/4 ComfyUI servisi"
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
for _ in $(seq 1 60); do curl -fs "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1 && break; sleep 5; done
curl -fs "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1 && step "ComfyUI yanıt veriyor (127.0.0.1:$PORT)" \
  || step "HATA: ComfyUI 5 dakikada yanıt vermedi (supervisorctl tail comfyui)"
phase_end
rm -f "$WORKSPACE/.hellgrind-setup-running" 2>/dev/null || true

cert_fp() {  # portalın dışarıya sunduğu sertifika (IP ile bağlanılır: SNI yok)
  local fp f
  fp=$(echo | timeout 10 openssl s_client -connect 127.0.0.1:8188 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
  if [ -z "$fp" ]; then
    for f in /etc/instance.crt /etc/caddy/instance.crt; do
      [ -s "$f" ] && fp=$(openssl x509 -in "$f" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2) && break
    done
  fi
  echo "${fp:-okunamadı}"
}
step "portal sertifikası SHA-256: $(cert_fp)"
step "indirilen toplam: $(( DL_BYTES / 1073741824 )) GB ($(( DL_BYTES / 1048576 )) MB); toplam kurulum süresi: $(since "$START")"
if [ $ok -eq 0 ]; then step "kurulum bitti: ComfyUI 127.0.0.1:$PORT"; else step "kurulum eksik bitti: yukarıdaki HATA satırlarına bakın"; fi
step "KURULUM_BITTI durum=$([ $ok -eq 0 ] && echo ok || echo eksik) sure=$(( $(date +%s) - START ))"
exit $ok
