#!/bin/zsh
# Regenerate the home-screen app-icon rasters. They are build output that
# happens to be committed — no browser will rasterize an SVG for a home
# screen tile, so the PNGs have to exist in the tree. Never hand-edit them:
# change static/icon.svg (or icon-maskable.svg) and re-run this.
#
#   scripts/make-icons.sh
#
# The tab favicon is NOT touched here. static/favicon.svg and favicon.ico
# are the originals and stay hand-maintained; icon.svg is that same mark
# redrawn at tile scale. If the favicon design ever changes, carry it into
# icon.svg by hand and re-run this, then re-cut the .ico separately —
# regenerating it here re-encodes an otherwise identical 32x32 into four
# times the bytes.
#
# Needs librsvg:  brew install librsvg
set -eu

HERE=${0:a:h}
STATIC=${HERE:h}/static

whence -p rsvg-convert >/dev/null || {
  echo "error: rsvg-convert not found (brew install librsvg)" >&2
  exit 1
}

render() {  # render <src.svg> <px> <out.png>
  rsvg-convert -w "$2" -h "$2" "$STATIC/$1" -o "$STATIC/$3"
  echo "  $3  ${2}x${2}"
}

echo "app icon (static/icon.svg)"
# 180 is what iOS asks for; it downscales that tile for every other slot.
render icon.svg 180 apple-touch-icon.png
render icon.svg 192 icon-192.png
render icon.svg 512 icon-512.png
echo "maskable cut (static/icon-maskable.svg)"
render icon-maskable.svg 512 icon-maskable-512.png
