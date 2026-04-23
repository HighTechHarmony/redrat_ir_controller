#!/usr/bin/env bash
# Download the Vosk small English model and openWakeWord models.
# Run from the project root: bash scripts/download_models.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MODELS_DIR="$PROJECT_ROOT/models"

mkdir -p "$MODELS_DIR"

# ---------------------------------------------------------------------------
# Vosk small English model (~50 MB)
# ---------------------------------------------------------------------------
VOSK_MODEL_NAME="vosk-model-small-en-us-0.15"
VOSK_ZIP="$MODELS_DIR/${VOSK_MODEL_NAME}.zip"
VOSK_URL="https://alphacephei.com/vosk/models/${VOSK_MODEL_NAME}.zip"

if [ -d "$MODELS_DIR/$VOSK_MODEL_NAME" ]; then
    echo "[vosk] Model already present at models/$VOSK_MODEL_NAME — skipping."
else
    echo "[vosk] Downloading $VOSK_MODEL_NAME ..."
    curl -L --progress-bar -o "$VOSK_ZIP" "$VOSK_URL"
    echo "[vosk] Extracting ..."
    unzip -q "$VOSK_ZIP" -d "$MODELS_DIR"
    rm "$VOSK_ZIP"
    echo "[vosk] Done."
fi

# ---------------------------------------------------------------------------
# openWakeWord models (downloaded via the Python helper)
# ---------------------------------------------------------------------------
echo "[openwakeword] Installing openwakeword ..."
pip install openwakeword --quiet

echo "[openwakeword] Downloading pre-trained models ..."
python3 - <<'EOF'
import openwakeword
openwakeword.utils.download_models()
print("[openwakeword] Done.")
EOF

echo ""
echo "All models ready."
echo "Update config/config.yaml → voice.vosk_model_path if you chose a different model."
