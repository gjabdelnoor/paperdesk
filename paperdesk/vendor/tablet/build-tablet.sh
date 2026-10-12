#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
: "${RM_SDK:?Set RM_SDK to your external reMarkable SDK directory}"
SDK_PATH=$RM_SDK
unset LD_LIBRARY_PATH
# shellcheck disable=SC1090
source "$SDK_PATH/environment-setup-cortexa53-crypto-remarkable-linux"
$CC -O2 -ffile-prefix-map="$HERE"=tablet -ffile-prefix-map="$SDK_PATH"=remarkable-sdk \
  -ffreestanding -fno-builtin -nostdlib -static -Wl,-e,_start \
  "$HERE/touch-injector.c" -o "$HERE/bin/touch-injector-aarch64"
XOVI_BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$XOVI_BUILD_DIR"' EXIT
cmake -S "$HERE/xovi" -B "$XOVI_BUILD_DIR" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64 \
  "-DCMAKE_C_FLAGS=-ffile-prefix-map=$HERE=tablet -ffile-prefix-map=$SDK_PATH=remarkable-sdk"
cmake --build "$XOVI_BUILD_DIR" -j2
cp "$XOVI_BUILD_DIR/xovi.so" "$HERE/direct-paste/xovi.so"
$STRIP --strip-unneeded "$HERE/direct-paste/xovi.so"
RM_SDK=$SDK_PATH bash "$HERE/direct-paste/build.sh"
cp "$HERE/direct-paste/build/direct_paste.so" "$HERE/direct-paste/direct_paste.so"
sha256sum "$HERE/bin/"* "$HERE/direct-paste/"*.so
