#!/usr/bin/env bash
# Build the direct-paste extension for the reMarkable Paper Pro (aarch64).
#
#   ./build.sh           production build (insertion enabled) -> build/direct_paste.so
#   ./build.sh --probe   read-only diagnostics build          -> build/direct_paste_probe.so
#
# Requires the reMarkable 3.26 SDK (see INSTALL.md). Override its location
# with RM_SDK. The produced binary is pinned to one tablet firmware; the
# extension refuses to insert on any other version.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
DEFINES=(-DDIRECT_PASTE_PRODUCTION_INSERTION)
OUTPUT=direct_paste.so
if [[ ${1:-} == --probe ]]; then
  DEFINES=(-DDIRECT_PASTE_PROBE_ONLY)
  OUTPUT=direct_paste_probe.so
elif [[ $# -ne 0 ]]; then
  echo "usage: $0 [--probe]" >&2
  exit 2
fi
SDK="${RM_SDK:-$HOME/rm-sdk-3.26}"
ENV_FILE=$SDK/environment-setup-cortexa53-crypto-remarkable-linux
if [[ ! -f "$ENV_FILE" ]]; then
  echo "reMarkable SDK not found at $SDK (set RM_SDK; see INSTALL.md)" >&2
  exit 1
fi
unset LD_LIBRARY_PATH
# shellcheck disable=SC1090
source "$ENV_FILE"
INC=$SDKTARGETSYSROOT/usr/include
mkdir -p "$HERE/build"
$CC -O2 -ffile-prefix-map="$HERE"=direct-paste -ffile-prefix-map="$SDK"=remarkable-sdk -fPIC -c "$HERE/xovi.c" -o "$HERE/build/xovi.o"
$CXX -O2 -ffile-prefix-map="$HERE"=direct-paste -ffile-prefix-map="$SDK"=remarkable-sdk -std=c++17 -Wall -Wextra -fPIC -shared \
  "${DEFINES[@]}" \
  -I "$INC" -I "$INC/QtCore" -I "$INC/QtGui" -I "$INC/QtQuick" -I "$INC/QtQml" \
  "$HERE/extension.cpp" "$HERE/build/xovi.o" \
  -lQt6Quick -lQt6Qml -lQt6Gui -lQt6Core -lpthread \
  -o "$HERE/build/$OUTPUT"
$STRIP --strip-unneeded "$HERE/build/$OUTPUT"
echo "$HERE/build/$OUTPUT"
