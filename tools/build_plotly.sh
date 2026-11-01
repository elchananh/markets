#!/usr/bin/env bash
# Rebuilds assets/plotly-custom.min.js: Plotly.js 2.35.2 core (scatter) + bar + pie + heatmap, bundled with esbuild.
set -euo pipefail
OUT="$(cd "$(dirname "$0")/.." && pwd)/assets/plotly-custom.min.js"
TMP="$(mktemp -d)"; cd "$TMP"
npm init -y >/dev/null
npm install --no-audit --no-fund plotly.js@2.35.2 esbuild@0.24 >/dev/null
cat > entry.js <<'JS'
var Plotly = require('plotly.js/lib/core');
Plotly.register([require('plotly.js/lib/bar'), require('plotly.js/lib/pie'), require('plotly.js/lib/heatmap')]);
module.exports = Plotly;
JS
npx esbuild entry.js --bundle --minify --format=iife --global-name=Plotly --define:global=window --target=es2017 --log-level=warning --outfile=bundle.js
{ printf '/**\n* plotly.js (custom: scatter, bar, pie, heatmap - minified) v2.35.2\n* Copyright 2012-2024, Plotly, Inc.\n* All rights reserved.\n* Licensed under the MIT license\n*/\n'; cat bundle.js; } > "$OUT"
rm -rf "$TMP"
ls -l "$OUT"
