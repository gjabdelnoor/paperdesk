#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Rebuild PaperDesk's two open-source ARM64 artifacts using an installed reMarkable SDK.
# The SDK and tablet Qt/system libraries are external and are never redistributed here.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
: "${RM_SDK:?Set RM_SDK to the installed reMarkable SDK directory}"
unset LD_LIBRARY_PATH
# shellcheck disable=SC1090
source "$RM_SDK/environment-setup-cortexa53-crypto-remarkable-linux"
# SDK compiler values include target/sysroot flags and are intentionally split.
$CC -O2 -fno-stack-protector -nostdlib -static -Wl,--build-id=none \
  "-ffile-prefix-map=$HERE=/paperdesk/vendor/art" "-ffile-prefix-map=$SDKTARGETSYSROOT=/opt/remarkable-sdk/sysroot" \
  "$HERE/pen-bridge.c" -o "$HERE/pen-bridge"
$STRIP --strip-unneeded "$HERE/pen-bridge"
INC=$SDKTARGETSYSROOT/usr/include
mkdir -p "$HERE/viewport/prebuilt"
$CXX -O2 -std=c++17 -Wall -Wextra -fPIC -shared -Wl,--build-id=none \
  "-ffile-prefix-map=$HERE=/paperdesk/vendor/art" "-ffile-prefix-map=$SDKTARGETSYSROOT=/opt/remarkable-sdk/sysroot" \
  -I "$INC" -I "$INC/QtCore" -I "$INC/QtGui" -I "$INC/QtQuick" -I "$INC/QtQml" \
  "$HERE/viewport/extension.cpp" \
  -lQt6Quick -lQt6Qml -lQt6Gui -lQt6Core -lpthread \
  -o "$HERE/viewport/prebuilt/krita_viewport.so"
$STRIP --strip-unneeded "$HERE/viewport/prebuilt/krita_viewport.so"
sha256sum "$HERE/pen-bridge" "$HERE/viewport/prebuilt/krita_viewport.so"
