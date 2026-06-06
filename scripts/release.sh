#!/bin/bash
# Release helper for aaPanel-tomcat-plugin
# Usage: ./scripts/release.sh [major|minor|patch|X.Y.Z]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Read current version
INFO=plugin/javahost/info.json
CURRENT_VER=$(python3 -c "import json;print(json.load(open('$INFO'))['versions'])")
echo "Current version: $CURRENT_VER"

if [ -z "${1:-}" ]; then
  echo "Usage: $0 [major|minor|patch|X.Y.Z]"
  echo "  major  → $CURRENT_VER → $(echo "$CURRENT_VER" | awk -F. '{print $1+1".0.0"}')"
  echo "  minor  → $CURRENT_VER → $(echo "$CURRENT_VER" | awk -F. '{print $1"."$2+1".0"}')"
  echo "  patch  → $CURRENT_VER → $(echo "$CURRENT_VER" | awk -F. '{print $1"."$2"."$3+1}')"
  echo "  X.Y.Z  → $CURRENT_VER → X.Y.Z"
  exit 1
fi

case "$1" in
  major) NEW_VER=$(echo "$CURRENT_VER" | awk -F. '{print $1+1".0.0"}') ;;
  minor) NEW_VER=$(echo "$CURRENT_VER" | awk -F. '{print $1"."$2+1".0"}') ;;
  patch) NEW_VER=$(echo "$CURRENT_VER" | awk -F. '{print $1"."$2"."$3+1}') ;;
  *)     NEW_VER="$1" ;;
esac

echo "New version: $NEW_VER"
echo ""

# Pre-flight checks
if ! git diff --quiet; then
  echo "ERROR: Working tree is dirty. Commit or stash changes first."
  exit 1
fi

if ! git diff --staged --quiet; then
  echo "ERROR: Staging area is not empty. Commit or unstage first."
  exit 1
fi

echo "== Generating changelog =="
if git describe --tags --abbrev=0 2>/dev/null; then
  LAST_TAG=$(git describe --tags --abbrev=0)
  echo "Changes since $LAST_TAG:"
  git log --oneline "${LAST_TAG}..HEAD"
else
  echo "No previous tag found — this is the first release."
  git log --oneline HEAD
fi

echo ""
echo "== Files changed =="
if git describe --tags --abbrev=0 2>/dev/null; then
  git diff --stat "${LAST_TAG}..HEAD"
else
  echo "(First release — all files)"
fi

echo ""
echo "== Creating distributable ZIP =="
( cd plugin && zip -r "../javahost-v${NEW_VER}.zip" javahost \
    -x "*/__pycache__/*" "*.pyc" )

echo "ZIP created: javahost-v${NEW_VER}.zip"
ls -lh "javahost-v${NEW_VER}.zip"

echo ""
echo "== Next steps =="
echo "1. Verify the ZIP: unzip -l aaPanel-tomcat-plugin-v${NEW_VER}.zip"
echo "2. Commit & tag:"
echo "   git add info.json CHANGELOG.md"
echo "   git commit -m \"chore: release v${NEW_VER}\""
echo "   git tag -a v${NEW_VER} -m \"Release v${NEW_VER}\""
echo "   git push origin main --tags"
echo "3. GitHub Actions will create the release automatically from the tag."
echo ""
echo "Or trigger manually via: gh workflow run release.yml -f version=${NEW_VER}"
