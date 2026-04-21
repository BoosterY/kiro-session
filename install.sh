#!/usr/bin/env bash
set -e

SKILL_DIR="$HOME/.kiro/skills/session-manager"
BIN_DIR="$HOME/.local/bin"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing kiro-session v2..."

# 0. Check dependencies
MISSING=""
if ! command -v python3 >/dev/null 2>&1; then
    MISSING="$MISSING python3"
fi
if ! command -v kiro-cli >/dev/null 2>&1; then
    MISSING="$MISSING kiro-cli"
fi
if ! python3 -c "import venv" 2>/dev/null; then
    MISSING="$MISSING python3-venv"
fi
if [ -n "$MISSING" ]; then
    echo "  ✘ Missing dependencies:$MISSING"
    echo "  Install them first, then re-run this script."
    exit 1
fi
echo "  ✔ Dependencies OK (python3, kiro-cli, python3-venv)"

# 1. Copy skill to ~/.kiro/skills/
mkdir -p "$SKILL_DIR"
cp -r "$SCRIPT_DIR/"* "$SKILL_DIR/" 2>/dev/null || true
cp "$SCRIPT_DIR/.gitignore" "$SKILL_DIR/" 2>/dev/null || true
chmod +x "$SKILL_DIR/kiro-session"
echo "  ✔ Skill installed to $SKILL_DIR"

# 2. Set up Python venv with dependencies
VENV_DIR="$SKILL_DIR/.venv"
DEPS_OK=true
for pkg in simple-term-menu orjson yaml jieba; do
    "$VENV_DIR/bin/python3" -c "import $pkg" 2>/dev/null || DEPS_OK=false
done

if [ "$DEPS_OK" = true ] && [ -d "$VENV_DIR" ]; then
    echo "  ✔ Python venv already set up"
else
    echo "  Setting up Python venv..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet simple-term-menu orjson pyyaml jieba && echo "  ✔ Python venv ready" \
        || echo "  ⚠ Failed to set up venv. Run: python3 -m venv $VENV_DIR && $VENV_DIR/bin/pip install simple-term-menu orjson pyyaml jieba"
fi

# 3. Symlink CLI command
mkdir -p "$BIN_DIR"
ln -sf "$SKILL_DIR/kiro-session" "$BIN_DIR/kiro-session"
echo "  ✔ CLI command linked to $BIN_DIR/kiro-session"

# 4. Check PATH
if echo "$PATH" | tr ':' '\n' | grep -q "$BIN_DIR"; then
    echo "  ✔ $BIN_DIR is in PATH"
else
    echo "  ⚠ $BIN_DIR is not in PATH. Add to your shell profile:"
    echo "    export PATH=\"$BIN_DIR:\$PATH\""
fi

# 5. Verify
if command -v kiro-session >/dev/null 2>&1; then
    echo ""
    echo "Done! Run 'kiro-session' to get started."
else
    echo ""
    echo "Done! Restart your shell or run: export PATH=\"$BIN_DIR:\$PATH\""
    echo "Then run 'kiro-session' to get started."
fi
