#! /bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="${ROOT}/dashboard_src"
OUT="${ROOT}/dashboard/build"

cd "${SRC}"
export VITE_BASE_API=/
if command -v bun >/dev/null 2>&1; then
  bun run build
else
  npm run build
fi

mkdir -p "${OUT}"
rm -rf "${OUT:?}/"*
cp -a "${SRC}/build/." "${OUT}/"
cp "${OUT}/index.html" "${OUT}/404.html"

echo "Dashboard built: ${OUT}"
