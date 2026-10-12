#!/usr/bin/env bash
# Rebuild the freestanding, bounded experimental ink helper for Paper Pro.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
: "${RM_SDK:?Set RM_SDK to your installed reMarkable SDK directory}"
unset LD_LIBRARY_PATH
source "$RM_SDK/environment-setup-cortexa53-crypto-remarkable-linux"
$CC -O2 -fno-stack-protector -nostdlib -static -Wl,--build-id=none \
  "-ffile-prefix-map=$HERE=/paperdesk/vendor/ink" \
  "$HERE/stylus-injector.c" -o "$HERE/stylus-injector-aarch64"
$STRIP --strip-unneeded "$HERE/stylus-injector-aarch64"
sha256sum "$HERE/stylus-injector-aarch64"
