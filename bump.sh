#!/usr/bin/env bash
set -euo pipefail

# ─── Konfiguration ───────────────────────────────────────────────
REMOTE="origin"
BRANCH="main"

# ─── 1. Branch prüfen ────────────────────────────────────────────
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "$BRANCH" ]]; then
  echo "❌ Aktueller Branch ist '$CURRENT_BRANCH', erwartet '$BRANCH'."
  exit 1
fi

echo "✅ Branch: $BRANCH"

# ─── 2. Versionsnummern anzeigen ─────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while IFS= read -r pjson; do
  FOLDER_NAME=$(basename "$(dirname "$pjson")")
  VERSION=$(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' "$pjson" | head -1 | sed 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')
  echo "📦 $FOLDER_NAME v${VERSION}"
done < <(find "$SCRIPT_DIR" -maxdepth 2 -name "plugin.json" -not -path "*/.git/*")

# ─── 3. Code pushen (Gitea + GitHub) ─────────────────────────────
echo ""
for REMOTE_NAME in origin github; do
  if git remote get-url "$REMOTE_NAME" > /dev/null 2>&1; then
    echo "📤 Push auf $REMOTE_NAME $BRANCH ..."
    git push "$REMOTE_NAME" "$BRANCH"
    echo "✅ $REMOTE_NAME push erfolgreich."
  fi
done
echo ""
echo "🎉 Fertig! Workflows übernehmen ZIP-Erstellung und Package-Upload."
