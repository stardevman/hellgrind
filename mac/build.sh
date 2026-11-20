#!/bin/bash
# Hell Grind Mac uygulamasını ve DMG'sini derler. macOS + Xcode Command Line Tools gerekir.
#   ./mac/build.sh            → mac/dist/Hellgrind.dmg
# Uygulama, derlendiği commit'teki hat dosyalarını (git'te izlenen; .env asla) kendi içinde taşır.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
OUT="$ROOT/mac/dist"
BUILD="$ROOT/mac/build"
APP="$BUILD/Hell Grind.app"
rm -rf "$BUILD" "$OUT"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$OUT"

REV=$(git rev-parse --short HEAD)
COUNT=$(git rev-list --count HEAD)
VERSION="1.0.$COUNT"
DIRTY=""
if ! git diff --quiet HEAD -- backend frontend db comfy-workflows docker-compose.yml .env.example; then
  DIRTY="+yerel"
  echo "Uyarı: commit edilmemiş değişiklikler var; uygulamaya yalnızca commit edilmiş hâl girer ($REV)." >&2
fi

echo "› Swift derleniyor (arm64 + x86_64)"
for arch in arm64 x86_64; do
  swiftc -O -target "$arch-apple-macos12.0" -framework AppKit -framework WebKit \
    -o "$BUILD/Hellgrind-$arch" mac/Sources/main.swift
done
lipo -create -output "$APP/Contents/MacOS/Hellgrind" "$BUILD/Hellgrind-arm64" "$BUILD/Hellgrind-x86_64"

echo "› Info.plist ve simge"
sed -e "s/__VERSION__/$VERSION/" -e "s/__BUILD__/$COUNT/" mac/Resources/Info.plist > "$APP/Contents/Info.plist"
ICONSET="$BUILD/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s mac/Resources/icon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) mac/Resources/icon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

echo "› Hat dosyaları uygulamaya kopyalanıyor ($REV)"
STACK="$APP/Contents/Resources/stack"
mkdir -p "$STACK"
git archive --format=tar HEAD -- backend frontend db comfy-workflows docker-compose.yml .env.example Makefile README.md .gitignore \
  | tar -x -C "$STACK"
echo "$REV$DIRTY" > "$STACK/.bundle-version"
test ! -e "$STACK/.env" || { echo "HATA: .env pakete girmemeli" >&2; exit 1; }

echo "› İmzalanıyor (ad-hoc; Apple Developer hesabı olmadan)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep "$APP"

echo "› DMG oluşturuluyor"
DMGSRC="$BUILD/dmg"
mkdir -p "$DMGSRC"
cp -R "$APP" "$DMGSRC/"
ln -s /Applications "$DMGSRC/Applications"
cp mac/KURULUM.txt "$DMGSRC/Önce oku.txt"
hdiutil create -volname "Hell Grind" -srcfolder "$DMGSRC" -ov -format UDZO "$OUT/Hellgrind-$VERSION.dmg" >/dev/null
echo "✓ $OUT/Hellgrind-$VERSION.dmg"
