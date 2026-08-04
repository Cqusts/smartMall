#!/usr/bin/env bash
# 下载全部本地模型（约 120 GB）。统一走 ModelScope，国内速度显著快于 HuggingFace。
#
# 下载完成后生成 deploy/models.lock 记录版本，保证环境可复现。
# 模型更新必须是一次显式操作——上游模型更新会改变输出分布，
# 导致评测结果不可比（见 docs/14-infra.md）。
#
# 用法：
#   ./deploy/scripts/download-models.sh              # 下载常驻模型（约 7 GB，M0 够用）
#   ./deploy/scripts/download-models.sh --all        # 全部，含图像/视频/训练基座

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
MODELS_DIR="${MODELS_DIR:-/data/models}"
LOCK_FILE="$DEPLOY_DIR/models.lock"

ALL=0
[ "${1:-}" = "--all" ] && ALL=1

command -v modelscope >/dev/null 2>&1 || {
  echo "==> 安装 modelscope CLI"
  pip install -q modelscope
}

mkdir -p "$MODELS_DIR"

# 常驻模型（约 6 GB 显存，服务启动即加载，M0 只需这些）
RESIDENT=(
  "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch|funasr|FunASR ASR，带时间戳预测"
  "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch|funasr-vad|长音频 VAD，直播转写必需"
  "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch|funasr-punc|中文标点恢复"
  "iic/CosyVoice2-0.5B|cosyvoice2|TTS，Apache-2.0 可商用"
  "BAAI/bge-m3|bge-m3|Embedding，1024 维"
  "BAAI/bge-reranker-v2-m3|bge-reranker-v2-m3|重排"
)

# 独占轮转 / 全卡独占（不能与常驻模型同时加载，见 docs/14-infra.md）
HEAVY=(
  "Wan-AI/Wan2.2-TI2V-5B|wan2.2-ti2v-5b|视频生成，~22G 显存，全卡独占"
  "qwen/Qwen3-8B|qwen3-8b|微调基座，QLoRA ~20G，全卡独占"
  "Qwen/Qwen-Image|comfyui/checkpoints/qwen-image|图像生成，中文文字渲染强，Apache-2.0"
)

download() {
  local model=$1 subdir=$2 desc=$3
  local target="$MODELS_DIR/$subdir"
  if [ -d "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null)" ]; then
    echo "  · 已存在，跳过：$subdir"
    return
  fi
  echo "  ↓ $desc"
  echo "    $model → $target"
  modelscope download --model "$model" --local_dir "$target"
}

echo "==> 下载常驻模型到 $MODELS_DIR"
for entry in "${RESIDENT[@]}"; do
  IFS='|' read -r m d desc <<< "$entry"
  download "$m" "$d" "$desc"
done

if [ $ALL -eq 1 ]; then
  echo
  echo "==> 下载重模型（约 70 GB，耗时较长）"
  for entry in "${HEAVY[@]}"; do
    IFS='|' read -r m d desc <<< "$entry"
    download "$m" "$d" "$desc"
  done
  echo
  echo "⚠️  FLUX / CatVTON / ControlNet / IP-Adapter 需手动放入"
  echo "    $MODELS_DIR/comfyui/{checkpoints,controlnet,ipadapter,catvton}/"
  echo "    注意 FLUX.1-dev 为非商用许可，商用请改用 FLUX.1-schnell 或 Qwen-Image"
fi

echo
echo "==> 生成 $LOCK_FILE"
{
  echo "# smartMall 模型版本锁定文件"
  echo "# 生成于 $(date -Iseconds)"
  echo "# 模型更新必须是显式操作：改这里 → 重新下载 → 重跑评测"
  echo
  for entry in "${RESIDENT[@]}" "${HEAVY[@]}"; do
    IFS='|' read -r m d desc <<< "$entry"
    target="$MODELS_DIR/$d"
    if [ -d "$target" ]; then
      size=$(du -sh "$target" 2>/dev/null | cut -f1)
      echo "$m|$d|$size|$desc"
    fi
  done
} > "$LOCK_FILE"

cat "$LOCK_FILE"
echo
echo "✅ 完成"
