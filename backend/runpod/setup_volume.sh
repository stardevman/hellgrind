#!/bin/bash
# Hell Grind · MiniMax H3 modellerini RunPod Network Volume'a BİR KEZ indirir.
#
# Nerede: volume'a bağlı geçici bir RunPod Pod'unda, elle (Pod'da volume /workspace'e bağlanır; serverless worker'da aynı
# volume /runpod-volume'dadır). Volume'un veri merkezi izinli listede olmalı (varsayılan CA-MTL-1; bkz. backend/runpod/README.md).
#
#   bash setup_volume.sh                 # modeller (paralel indirme)
#   bash setup_volume.sh --comfyui       # + test ve iş akışı dışa aktarımı için ComfyUI'yi kurup 8188'de başlatır
#
# Yerleşim (worker'ın extra_model_paths.yaml'ı bunu okur; ComfyUI'nin klasör adları):
#   <volume>/models/unet  clip  vae  loras  checkpoints
# Video VAE fp16'dır (iş akışları minimax_h3_video_vae_fp16 bekler; int8_convrot VAE cu130 ister). fp8_scaled DiT'ler
# int8_convrot'un yedeğidir (HG_H3_FP8_FALLBACK=0 ile atlanır).
# İndirme: huggingface_hub `hf download` (Xet; HF_XET_HIGH_PERFORMANCE=1) ya da HG_DOWNLOADER=aria2c (16 bağlantı). Dosyalar
# HG_PARALLEL (varsayılan 4) adet aynı anda iner; inmiş dosya atlanır, kesilen indirme yeniden çalıştırınca sürer.
# Ortam: HG_IMAGE_MODEL (qwen-image-2512 | flux-schnell | none), HG_H3_TEXT_ENCODER, HF_TOKEN, HG_VOLUME (varsayılan /workspace).
# Günlük: <volume>/hellgrind/setup_volume.log (aşama ve dosya başına süre, hız, sonda toplam süre ve boyut).
set -uo pipefail

VOLUME=${HG_VOLUME:-/workspace}
M="$VOLUME/models"
LOGDIR="$VOLUME/hellgrind"
mkdir -p "$LOGDIR" "$M"/{unet,clip,vae,loras,checkpoints}
LOG="$LOGDIR/setup_volume.log"
exec > >(tee -a "$LOG") 2>&1
START=$(date +%s)
IMAGE_MODEL=${HG_IMAGE_MODEL:-qwen-image-2512}
TEXT_ENCODER=${HG_H3_TEXT_ENCODER:-qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors}
DOWNLOADER=${HG_DOWNLOADER:-hf}
PARALLEL=${HG_PARALLEL:-4}
HF=https://huggingface.co
WITH_COMFY=0
[ "${1:-}" = "--comfyui" ] && WITH_COMFY=1

since() { local s=$(( $(date +%s) - $1 )); printf '%d dk %02d sn' $((s / 60)) $((s % 60)); }
step() { echo "[hellgrind $(date +%H:%M:%S) +$(since "$START")] $*"; }

step "volume kurulumu başladı ($VOLUME; görsel modeli: $IMAGE_MODEL; indirici: $DOWNLOADER, $PARALLEL paralel)"
step "volume: $(df -BG --output=size,avail "$VOLUME" 2>/dev/null | tail -1 | awk '{print "boyut " $1 ", boş " $2}')"

python3 -m pip install -q -U "huggingface_hub[hf_xet]" 2>/dev/null || step "huggingface_hub güncellenemedi"
if [ "$DOWNLOADER" = "aria2c" ] && ! command -v aria2c >/dev/null; then
  (apt-get update -qq && apt-get install -y -qq aria2) >/dev/null 2>&1 || { step "aria2c kurulamadı; hf ile sürülüyor"; DOWNLOADER=hf; }
fi
export HF_XET_HIGH_PERFORMANCE=1 HF_HUB_ENABLE_HF_TRANSFER=0 HF_HUB_DISABLE_PROGRESS_BARS=1

fetch() {  # depo  depodaki-yol  hedef-klasör
  local repo=$1 path=$2 dir=$3 name t0 secs size tmp; name=$(basename "$path")
  if [ -s "$dir/$name" ]; then step "var: $name"; return 0; fi
  step "indiriliyor: $repo/$path"
  t0=$(date +%s)
  if [ "$DOWNLOADER" = "aria2c" ]; then
    aria2c -q -x 16 -s 16 -k 64M -c --file-allocation=none ${HF_TOKEN:+--header="Authorization: Bearer $HF_TOKEN"} \
      -d "$dir" -o "$name.part" "$HF/$repo/resolve/main/$path" && mv "$dir/$name.part" "$dir/$name" \
      || { step "HATA: indirilemedi: $path"; return 1; }
  else
    tmp="$dir/.hf-$name"
    hf download "$repo" "$path" --local-dir "$tmp" >/dev/null && mv "$tmp/$path" "$dir/$name" && rm -rf "$tmp" \
      || { step "HATA: indirilemedi: $path"; return 1; }
  fi
  size=$(stat -c %s "$dir/$name" 2>/dev/null || echo 0)
  secs=$(( $(date +%s) - t0 )); [ "$secs" -gt 0 ] || secs=1
  step "indi: $name ($(( size / 1048576 )) MB, $secs sn, $(( size / 1048576 / secs )) MB/sn)"
}

H3=Comfy-Org/MiniMax-H3
QW=Comfy-Org/Qwen-Image_ComfyUI
JOBS=(
  "$H3 diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors $M/unet"
  "$H3 diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors $M/unet"
  "$H3 text_encoders/$TEXT_ENCODER $M/clip"
  "$H3 vae/minimax_h3_video_vae_fp16.safetensors $M/vae"
  "$H3 vae/minimax_h3_audio_vae_fp32.safetensors $M/vae"
  "$H3 loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors $M/loras"
  "$H3 loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors $M/loras"
)
if [ "${HG_H3_FP8_FALLBACK:-1}" = "1" ]; then
  JOBS+=("$H3 diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors $M/unet"
         "$H3 diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors $M/unet")
fi
case "$IMAGE_MODEL" in
  qwen-image-2512)          # Apache 2.0 (Qwen/Qwen-Image-2512)
    JOBS+=("$QW split_files/diffusion_models/qwen_image_2512_fp8_e4m3fn.safetensors $M/unet"
           "$QW split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors $M/clip"
           "$QW split_files/vae/qwen_image_vae.safetensors $M/vae") ;;
  flux-schnell)             # Apache 2.0; FLUX.1 [dev] ticari olmayan lisanslıdır, kurulmaz
    JOBS+=("Comfy-Org/flux1-schnell flux1-schnell-fp8.safetensors $M/checkpoints") ;;
  none) step "görsel modeli kurulmadı" ;;
  *) step "HATA: bilinmeyen görsel modeli: $IMAGE_MODEL"; exit 1 ;;
esac

T0=$(date +%s)
step "== Modeller (${#JOBS[@]} dosya) =="
FAILED="$LOGDIR/.failed"; : > "$FAILED"
for job in "${JOBS[@]}"; do
  while [ "$(jobs -rp | wc -l)" -ge "$PARALLEL" ]; do sleep 2; done
  # shellcheck disable=SC2086
  (fetch $job || echo "$job" >> "$FAILED") &
done
wait
step "== Modeller bitti ($(( $(date +%s) - T0 )) sn) =="
TOTAL=$(du -sb "$M" 2>/dev/null | cut -f1)
step "volume'daki modeller: $(( ${TOTAL:-0} / 1073741824 )) GB; boş: $(df -BG --output=avail "$VOLUME" | tail -1 | tr -d ' ')"
ok=0
if [ -s "$FAILED" ]; then ok=1; step "HATA: inmeyen dosyalar (yeniden çalıştırın):"; cat "$FAILED"; fi

if [ $WITH_COMFY -eq 1 ]; then
  step "== ComfyUI (test ve iş akışı dışa aktarımı) =="
  C="$VOLUME/pod-comfyui"
  REF=${HG_COMFYUI_REF:-v0.39.0}          # worker'la aynı sürüm (iş akışları bu sürümden dışa aktarıldı)
  [ -d "$C/.git" ] || git clone https://github.com/comfyanonymous/ComfyUI "$C" || { step "HATA: ComfyUI indirilemedi"; exit 1; }
  git -C "$C" fetch --depth 1 origin "$REF" && git -C "$C" checkout -f FETCH_HEAD || step "HATA: ComfyUI $REF alınamadı"
  [ -x "$C/venv/bin/python" ] || python3 -m venv "$C/venv"
  "$C/venv/bin/python" -m pip install -q --upgrade pip
  "$C/venv/bin/python" -m pip install -q torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
  "$C/venv/bin/python" -m pip install -q -r "$C/requirements.txt"
  cat > "$C/extra_model_paths.yaml" <<YAML
hellgrind:
  base_path: $VOLUME/models
  unet: unet
  diffusion_models: unet
  clip: clip
  text_encoders: clip
  vae: vae
  loras: loras
  checkpoints: checkpoints
YAML
  nohup "$C/venv/bin/python" "$C/main.py" --listen 0.0.0.0 --port 8188 > "$LOGDIR/pod-comfyui.log" 2>&1 &
  step "ComfyUI başlatıldı: Pod'un 8188 HTTP portu (RunPod panelindeki Connect bağlantısı). İş akışlarını Workflow › Export (API) ile kaydedin."
fi

step "toplam süre: $(since "$START")"
step "VOLUME_KURULUMU_BITTI durum=$([ $ok -eq 0 ] && echo ok || echo eksik) sure=$(( $(date +%s) - START ))"
exit $ok
