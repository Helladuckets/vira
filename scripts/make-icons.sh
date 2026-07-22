#!/bin/zsh
# Regenerate every raster icon from the two SVG masters. The PNGs and the
# .ico are build output that happens to be committed (a browser cannot
# rasterize an SVG for a home-screen tile), so they are never hand-edited —
# change static/icon.svg or static/favicon.svg and re-run this.
#
#   scripts/make-icons.sh
#
# Needs librsvg + ImageMagick:  brew install librsvg imagemagick
set -eu

HERE=${0:a:h}
STATIC=${HERE:h}/static

for bin in rsvg-convert magick; do
  whence -p $bin >/dev/null || {
    echo "error: $bin not found (brew install librsvg imagemagick)" >&2
    exit 1
  }
done

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

echo "favicon (static/favicon.svg)"
rsvg-convert -w 16 -h 16 "$STATIC/favicon.svg" -o "$STATIC/.fav16.png"
rsvg-convert -w 32 -h 32 "$STATIC/favicon.svg" -o "$STATIC/.fav32.png"
magick "$STATIC/.fav16.png" "$STATIC/.fav32.png" "$STATIC/favicon.ico"
rm -f "$STATIC/.fav16.png" "$STATIC/.fav32.png"
echo "  favicon.ico  16x16 + 32x32"
