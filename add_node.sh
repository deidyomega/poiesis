#!/usr/bin/env bash
set -euo pipefail

# ── Glitch Core — Add Worker Node ──────────────────────────────────────────
# Run this on any machine to add it as a worker node.

GLITCH_HOME="$HOME/.glitch"

echo "╔══════════════════════════════════════╗"
echo "║     Glitch Core — Add Node           ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── Prerequisites ──────────────────────────────────────────────────────────

if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3.11+ is required."
    exit 1
fi

# ── Collect Info ───────────────────────────────────────────────────────────

read -rp "Firebase Project ID: " FIREBASE_PROJECT
if [ -z "$FIREBASE_PROJECT" ]; then
    echo "ERROR: Firebase project ID is required."
    exit 1
fi

read -rp "Path to service account JSON key: " CREDS_PATH
CREDS_PATH="${CREDS_PATH/#\~/$HOME}"
if [ ! -f "$CREDS_PATH" ]; then
    echo "ERROR: File not found: $CREDS_PATH"
    exit 1
fi

read -rp "Node name: " NODE_NAME
if [ -z "$NODE_NAME" ]; then
    echo "ERROR: Node name is required."
    exit 1
fi

echo ""
echo "What capabilities does this node have?"
echo "  api    — Can call cloud AI APIs (Gemini, Anthropic)"
echo "  local  — Has Ollama for local model inference"
echo "  gpu    — Has GPU for heavy local inference"
echo "  tailnet — Can SSH to other Tailscale nodes"
echo ""
read -rp "Capabilities (comma-separated, e.g. api,local): " CAPS_RAW
CAPS_RAW="${CAPS_RAW:-api}"

# Format as JSON array
IFS=',' read -ra CAPS_ARRAY <<< "$CAPS_RAW"
CAPS_JSON="["
for i in "${!CAPS_ARRAY[@]}"; do
    cap=$(echo "${CAPS_ARRAY[$i]}" | xargs)
    if [ $i -gt 0 ]; then CAPS_JSON+=","; fi
    CAPS_JSON+="\"$cap\""
done
CAPS_JSON+="]"

# ── Optional API Keys ─────────────────────────────────────────────────────

echo ""
echo "── API Keys (optional — press Enter to skip) ──"
read -rp "Google Gemini API Key: " GEMINI_KEY
read -rp "Anthropic API Key: " ANTHROPIC_KEY
read -rp "Ollama Host (e.g. http://localhost:11434): " OLLAMA_HOST

# ── Write Config ──────────────────────────────────────────────────────────

mkdir -p "$GLITCH_HOME"

cp "$CREDS_PATH" "$GLITCH_HOME/credentials.json"
chmod 600 "$GLITCH_HOME/credentials.json"

cat > "$GLITCH_HOME/.env" << EOF
GLITCH_FIREBASE_PROJECT=$FIREBASE_PROJECT
GLITCH_FIREBASE_CREDENTIALS=$GLITCH_HOME/credentials.json
GLITCH_NODE_NAME=$NODE_NAME
GLITCH_NODE_CAPABILITIES=$CAPS_JSON
EOF

if [ -n "$GEMINI_KEY" ]; then
    echo "GLITCH_GEMINI_API_KEY=$GEMINI_KEY" >> "$GLITCH_HOME/.env"
fi
if [ -n "$ANTHROPIC_KEY" ]; then
    echo "GLITCH_ANTHROPIC_API_KEY=$ANTHROPIC_KEY" >> "$GLITCH_HOME/.env"
fi
if [ -n "$OLLAMA_HOST" ]; then
    echo "GLITCH_OLLAMA_HOST=$OLLAMA_HOST" >> "$GLITCH_HOME/.env"
fi

chmod 600 "$GLITCH_HOME/.env"

echo ""
echo "Node '$NODE_NAME' configured."
echo ""
echo "Next steps:"
echo "  1. Clone the repo on this machine"
echo "  2. pip install -e ."
echo "  3. glitch start   (to run as a worker)"
echo ""
