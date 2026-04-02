#!/usr/bin/env bash
set -euo pipefail

# ── Glitch Core Installer ──────────────────────────────────────────────────
# Sets up a fresh Glitch Core installation on the primary node.

GLITCH_HOME="$HOME/.glitch"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "╔══════════════════════════════════════╗"
echo "║        Glitch Core Installer         ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── Prerequisites Check ────────────────────────────────────────────────────

echo "Checking prerequisites..."

if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 is required but not found."
    echo "Install Python 3.11+ from https://python.org"
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]); then
    echo "ERROR: Python 3.11+ required. Found Python $PYTHON_VERSION"
    exit 1
fi
echo "  Python $PYTHON_VERSION ✓"

if ! command -v pip3 &>/dev/null && ! python3 -m pip --version &>/dev/null; then
    echo "ERROR: pip is required but not found."
    exit 1
fi
echo "  pip ✓"

# ── Firebase Project ───────────────────────────────────────────────────────

echo ""
echo "── Firebase Setup ──"
echo ""
echo "You need a Firebase project with Firestore enabled."
echo "If you don't have one yet:"
echo "  1. Go to https://console.firebase.google.com"
echo "  2. Create a new project"
echo "  3. Enable Firestore (Native mode)"
echo "  4. Create a service account key:"
echo "     Project Settings → Service Accounts → Generate New Private Key"
echo ""

read -rp "Firebase Project ID: " FIREBASE_PROJECT
if [ -z "$FIREBASE_PROJECT" ]; then
    echo "ERROR: Firebase project ID is required."
    exit 1
fi

# ── Service Account Key ───────────────────────────────────────────────────

read -rp "Path to service account JSON key: " CREDS_PATH
CREDS_PATH="${CREDS_PATH/#\~/$HOME}"

if [ ! -f "$CREDS_PATH" ]; then
    echo "ERROR: File not found: $CREDS_PATH"
    exit 1
fi

# ── API Keys ──────────────────────────────────────────────────────────────

echo ""
echo "── API Keys (optional — press Enter to skip) ──"
echo ""

read -rp "Google Gemini API Key: " GEMINI_KEY
read -rp "Anthropic API Key: " ANTHROPIC_KEY
read -rp "Ollama Host (e.g. http://localhost:11434): " OLLAMA_HOST

# ── Node Configuration ────────────────────────────────────────────────────

echo ""
read -rp "Node name [main]: " NODE_NAME
NODE_NAME="${NODE_NAME:-main}"

# ── Create Glitch Home ────────────────────────────────────────────────────

echo ""
echo "Setting up $GLITCH_HOME..."

mkdir -p "$GLITCH_HOME"

# Copy credentials
cp "$CREDS_PATH" "$GLITCH_HOME/credentials.json"
chmod 600 "$GLITCH_HOME/credentials.json"
echo "  Copied credentials ✓"

# Write .env file
cat > "$GLITCH_HOME/.env" << EOF
GLITCH_FIREBASE_PROJECT=$FIREBASE_PROJECT
GLITCH_FIREBASE_CREDENTIALS=$GLITCH_HOME/credentials.json
GLITCH_NODE_NAME=$NODE_NAME
GLITCH_NODE_CAPABILITIES=["api"]
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
echo "  Created .env ✓"

# ── Python Virtual Environment ────────────────────────────────────────────

echo ""
echo "Creating Python virtual environment..."

cd "$REPO_DIR"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate
echo "  venv activated ✓"

# ── Install Dependencies ──────────────────────────────────────────────────

echo "Installing dependencies..."
pip install -e ".[dev]" --quiet
echo "  Dependencies installed ✓"

# ── Bootstrap Firestore ───────────────────────────────────────────────────

echo ""
echo "Bootstrapping Firestore..."
python -m glitch_core.bootstrap
echo "  Firestore bootstrapped ✓"

# ── Done ──────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════╗"
echo "║       Installation Complete!         ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "To start Glitch Core:"
echo "  cd $REPO_DIR"
echo "  source .venv/bin/activate"
echo "  glitch start"
echo ""
echo "Then open http://localhost:8080 in your browser."
echo ""
