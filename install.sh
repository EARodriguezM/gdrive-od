#!/usr/bin/env bash
# gdrive-od installer: dependencies, the two programs, desktop integration, and your first account.
#
#   ./install.sh                  install (asks before using sudo for packages)
#   ./install.sh --no-deps        don't install packages (you already have them)
#   ./install.sh --prefix DIR     install the programs into DIR/bin (default ~/.local)
#   ./install.sh --no-ui          skip the tray app (no PySide6 needed)
#   ./install.sh --no-account     don't offer to connect a Google account at the end
#   ./install.sh --uninstall      remove the programs and desktop integration (keeps your files)
set -euo pipefail

PREFIX="${HOME}/.local"
DEPS=1 UI=1 ACCOUNT=1 UNINSTALL=0 YES=0
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF_DIR="$XDG_CONFIG_HOME/gdrive-ondemand"
MIN_PYTHON="3.11"
MIN_RCLONE="1.60"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m ✗\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { # ask "question" default(y/n) -> 0 for yes
    local q="$1" d="$2" a
    [[ $YES == 1 ]] && return 0
    [[ -t 0 ]] || { [[ $d == y ]]; return; }
    read -r -p "$q [$( [[ $d == y ]] && echo Y/n || echo y/N )] " a || true
    a="${a:-$d}"; [[ ${a,,} == y* ]]
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --prefix=*) PREFIX="${1#*=}"; shift ;;
        --no-deps) DEPS=0; shift ;;
        --no-ui) UI=0; shift ;;
        --no-account) ACCOUNT=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -y|--yes) YES=1; shift ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done
BIN="$PREFIX/bin"

# ─────────────────────────── uninstall ───────────────────────────
if [[ $UNINSTALL == 1 ]]; then
    say "Stopping the sync services"
    names=()
    if [[ -f $CONF_DIR/config.toml ]] && command -v python3 >/dev/null; then
        mapfile -t names < <(python3 -c 'import sys, tomllib; print("\n".join(tomllib.load(open(sys.argv[1], "rb")).get("accounts", {})))' "$CONF_DIR/config.toml" 2>/dev/null || true)
    fi
    for f in "$XDG_CONFIG_HOME"/systemd/user/default.target.wants/gdrive-od@*.service; do
        [[ -e $f || -L $f ]] && names+=("$(basename "$f" .service | sed 's/^gdrive-od@//')")
    done
    if command -v systemctl >/dev/null; then
        for n in $(printf '%s\n' "${names[@]}" | sort -u); do
            [[ -n $n ]] && systemctl --user disable --now "gdrive-od@$n" 2>/dev/null && ok "stopped gdrive-od@$n"
        done
    fi
    pkill -f "^python3 $BIN/gdrive-od-ui" 2>/dev/null || true
    say "Removing the programs and desktop integration"
    rm -f "$BIN/gdrive-od" "$BIN/gdrive-od-ui" \
          "$XDG_DATA_HOME/applications/gdrive-od-open.desktop" "$XDG_DATA_HOME/applications/gdrive-od-ui.desktop" \
          "$XDG_DATA_HOME/kio/servicemenus/gdrive-od.desktop" "$XDG_DATA_HOME/mime/packages/gdrive-ondemand.xml" \
          "$XDG_CONFIG_HOME/systemd/user/gdrive-od@.service" "$XDG_CONFIG_HOME/autostart/gdrive-od-ui.desktop" \
          "$XDG_DATA_HOME"/icons/hicolor/scalable/mimetypes/application-vnd.google-apps.{document,spreadsheet,presentation,form,drawing,map,site,script,unknown}.svg
    command -v update-mime-database >/dev/null && update-mime-database "$XDG_DATA_HOME/mime" >/dev/null 2>&1 || true
    command -v update-desktop-database >/dev/null && update-desktop-database "$XDG_DATA_HOME/applications" >/dev/null 2>&1 || true
    command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
    ok "Uninstalled."
    cat <<EOF
Kept (delete them yourself if you want):
  your Drive folders (e.g. ~/GoogleDrive) and their placeholders
  the config            $CONF_DIR
  the sync state        ${XDG_STATE_HOME:-$HOME/.local/state}/gdrive-ondemand
  the rclone remotes    rclone config   (or ~/.config/rclone/rclone.conf)
  Dolphin Places entries "Google Drive · …" (right-click › Remove)
EOF
    exit 0
fi

[[ $EUID -ne 0 ]] || die "Run this as your normal user, not root (it installs into your home)."
[[ -f $HERE/gdrive-od ]] || die "Run install.sh from the gdrive-od folder (gdrive-od not found next to it)."

# ─────────────────────────── dependencies ───────────────────────────
pkgs=() ui_pkgs=() pm=()
if command -v pacman >/dev/null; then
    pm=(sudo pacman -S --needed); pkgs=(rclone python libnotify xdg-utils); ui_pkgs=(pyside6)
elif command -v apt-get >/dev/null; then
    pm=(sudo apt-get install -y); pkgs=(rclone python3 libnotify-bin xdg-utils)
    ui_pkgs=(python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets python3-pyside6.qtnetwork)
elif command -v dnf >/dev/null; then
    pm=(sudo dnf install -y); pkgs=(rclone python3 libnotify xdg-utils); ui_pkgs=(python3-pyside6)
elif command -v zypper >/dev/null; then
    pm=(sudo zypper install -y); pkgs=(rclone python3 libnotify-tools xdg-utils); ui_pkgs=(python3-pyside6)
fi

if [[ $DEPS == 1 ]]; then
    if [[ ${#pm[@]} -eq 0 ]]; then
        warn "Unknown package manager: install rclone (≥ $MIN_RCLONE), python3 (≥ $MIN_PYTHON), notify-send and xdg-utils yourself."
    else
        say "Packages: ${pkgs[*]}$( [[ $UI == 1 ]] && echo " (+ ${ui_pkgs[*]} for the tray app)")"
        if ask "Install them now with '${pm[*]}'?" y; then
            "${pm[@]}" "${pkgs[@]}" || die "Package installation failed."
            if [[ $UI == 1 ]]; then
                "${pm[@]}" "${ui_pkgs[@]}" || warn "Couldn't install PySide6: the tray app will be skipped (syncing works without it)."
            fi
        fi
    fi
fi

version_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]; }
command -v python3 >/dev/null || die "python3 is missing."
pyv="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
version_ge "$pyv" "$MIN_PYTHON" || die "Python $pyv is too old: gdrive-od needs Python ≥ $MIN_PYTHON."
ok "Python $pyv"
command -v rclone >/dev/null || die "rclone is missing (https://rclone.org/install/)."
rv="$(rclone version 2>/dev/null | head -1 | sed -E 's/^rclone v?([0-9.]+).*/\1/')"
if version_ge "$rv" "$MIN_RCLONE"; then
    ok "rclone $rv"
else
    warn "rclone $rv is older than $MIN_RCLONE (your distribution's package may lag). Get the current one with:"
    warn "  sudo -v && curl https://rclone.org/install.sh | sudo bash"
    ask "Continue anyway?" n || exit 1
fi
command -v notify-send >/dev/null || warn "notify-send not found: no desktop notifications."
if [[ $UI == 1 ]] && ! python3 -c 'import PySide6.QtWidgets, PySide6.QtNetwork' 2>/dev/null; then
    warn "PySide6 is not available: skipping the tray app (install it and run ./install.sh again to add it)."
    UI=0
fi

# ─────────────────────────── programs and desktop ───────────────────────────
say "Installing into $BIN"
install -Dm755 "$HERE/gdrive-od" "$BIN/gdrive-od"
ok "gdrive-od"
if [[ $UI == 1 ]]; then
    install -Dm755 "$HERE/gdrive-od-ui" "$BIN/gdrive-od-ui"
    ok "gdrive-od-ui (tray app)"
else
    rm -f "$BIN/gdrive-od-ui"
fi
"$BIN/gdrive-od" setup-desktop | sed 's/^/   /'
if [[ $UI == 1 ]]; then
    mkdir -p "$XDG_CONFIG_HOME/autostart"
    cat > "$XDG_CONFIG_HOME/autostart/gdrive-od-ui.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Google Drive status
Exec=$BIN/gdrive-od-ui --tray
Icon=folder-gdrive
X-GNOME-Autostart-enabled=true
EOF
    ok "the tray icon starts at login (untick it in the app's Settings to stop that)"
fi
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "$BIN is not on your PATH. Add it, e.g. for bash: echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.bashrc"
       warn "  fish: fish_add_path $BIN" ;;
esac

# ─────────────────────────── first account ───────────────────────────
if [[ $ACCOUNT == 1 && -t 0 ]] && ask "Connect a Google account now?" y; then
    client="$CONF_DIR/oauth-client.json"
    if [[ ! -f $client ]]; then
        echo
        echo "  You need your own Google OAuth client (a JSON file) first; it takes about 5 minutes:"
        echo "  see $HERE/docs/google-oauth-client.md"
        read -r -p "  Path of the downloaded client JSON (client_secret_….json): " client
        client="${client/#\~/$HOME}"
        [[ -f $client ]] || die "Not found: $client. When you have it: gdrive-od add-account NAME --email ADDRESS --client FILE"
    fi
    read -r -p "  Short name for the account (e.g. personal, work): " name
    read -r -p "  Google account email: " email
    "$BIN/gdrive-od" add-account "$name" --email "$email" --client "$client"
    [[ $UI == 1 ]] && (setsid -f "$BIN/gdrive-od-ui" >/dev/null 2>&1 </dev/null || true)
fi

echo
ok "Installed."
cat <<EOF
   Add (more) Google accounts:   gdrive-od add-account NAME --email ADDRESS
   Your files:                   ~/GoogleDrive/<Name>   (also in Dolphin's Places panel)
   Status window:                "Google Drive" in the application menu$( [[ $UI == 1 ]] || echo " (needs PySide6)")
   Logs:                         journalctl --user -fu gdrive-od@NAME
EOF
