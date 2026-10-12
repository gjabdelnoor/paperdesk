#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Kept as a convenience entrypoint; PaperDesk builds both matching tablet artifacts.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
exec bash "$HERE/../build-tablet.sh" "$@"
