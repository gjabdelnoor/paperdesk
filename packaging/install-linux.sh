#!/usr/bin/env bash
# Install this portable bundle for the current user. Existing installs are archived.
set -euo pipefail
src=$(cd -- "$(dirname -- "$0")" && pwd)
test -x "$src/PaperDesk" || { echo 'Place install.sh beside the PaperDesk executable.' >&2; exit 1; }
pd_lib="${XDG_DATA_HOME:-$HOME/.local/share}/paperdesk-desktop"
pd_backup="${XDG_STATE_HOME:-$HOME/.local/state}/PaperDesk/desktop-backups"
pd_apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
if [[ -e "$pd_lib" ]]; then
  [[ -f "$pd_lib/.paperdesk-desktop-install" && ! -L "$pd_lib" ]] || { echo 'Destination is not an owned PaperDesk installation.' >&2; exit 1; }
fi
mkdir -p "$(dirname -- "$pd_lib")" "$pd_backup" "$pd_apps"
pd_stage=$(mktemp -d "$(dirname -- "$pd_lib")/paperdesk-stage.XXXXXX")
trap 'rm -rf -- "$pd_stage"' EXIT
cp -a -- "$src/." "$pd_stage/"
printf 'PaperDesk desktop bundle\n' > "$pd_stage/.paperdesk-desktop-install"
if [[ -d "$pd_lib" ]]; then mv -- "$pd_lib" "$pd_backup/$(date +%Y%m%d-%H%M%S)-$$"; fi
mv -- "$pd_stage" "$pd_lib"
# Escape characters with special meaning inside a quoted desktop Exec argument.
pd_exec=${pd_lib//\\/\\\\}
pd_exec=${pd_exec//\"/\\\"}
pd_exec=${pd_exec//\$/\\\$}
pd_exec=${pd_exec//\`/\\\`}
pd_exec=${pd_exec//%/%%}
cat > "$pd_apps/paperdesk.desktop" <<EOF
[Desktop Entry]
Name=PaperDesk
Comment=reMarkable tools and art in one desktop workspace
Exec="$pd_exec/PaperDesk"
Icon=accessories-text-editor
Terminal=false
Type=Application
Categories=Office;Graphics;
EOF
echo 'PaperDesk is installed in your applications menu.'
