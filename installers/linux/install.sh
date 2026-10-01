#!/bin/sh
# Install Ninaivu from an unpacked payload (header.sh unpacks it and calls this).
#
#   install.sh PAYLOAD [--prefix DIR] [--photos DIR] [--no-service] [--quiet]
#
# What it does, in order: copies the private Python and installs every bundled
# wheel into it, offline; makes the `ninaivu` command (and `ninaivu-tray` for a
# desktop); writes a desktop entry; sets up a systemd service that starts
# Ninaivu at boot (a user service, or a system one when run as root) pointed
# at the photographs folder; starts it; says where to open it. Run again to
# upgrade in place: the library, the settings and the index are kept, because
# they live under the state folder, not under the program.
set -e
payload=$1; shift
version=$(cat "$payload/VERSION")
prefix=""; photos=""; service=1; quiet=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) prefix=$2; shift 2 ;;
        --photos) photos=$2; shift 2 ;;
        --no-service) service=0; shift ;;
        --quiet) quiet=1; shift ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
say() { [ "$quiet" = 1 ] || echo "$@"; }

if [ "$(id -u)" = 0 ]; then
    default_prefix=/opt/ninaivu; bindir=/usr/local/bin; apps=/usr/local/share/applications; state=/var/lib/ninaivu
else
    default_prefix="${XDG_DATA_HOME:-$HOME/.local/share}/ninaivu"; bindir="$HOME/.local/bin"
    apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"; state="${XDG_STATE_HOME:-$HOME/.local/state}/ninaivu"
fi
prefix=${prefix:-$default_prefix}
say "Ninaivu $version → $prefix"

# The machine's own Python is not used, and it need not have one.
mkdir -p "$prefix" "$bindir" "$state"
rm -rf "$prefix/python.new"
cp -R "$payload/python" "$prefix/python.new"
py="$prefix/python.new/bin/python3"
"$py" -m pip install --quiet --no-index --no-deps --no-warn-script-location "$payload"/wheels/*.whl
# Only now is the old one replaced, so a failure above leaves a working install.
rm -rf "$prefix/python"
mv "$prefix/python.new" "$prefix/python"
cp "$payload/LICENSE" "$payload/README.md" "$prefix/"
printf '%s\n' "$version" > "$prefix/VERSION"

# The commands. NINAIVU_HOME is where the server keeps its state, its log and
# its run file, as the Windows and Mac installers set it.
cat > "$prefix/ninaivu" <<EOF
#!/bin/sh
export NINAIVU_HOME="$state"
exec "$prefix/python/bin/python3" -m ninaivu "\$@"
EOF
cat > "$prefix/ninaivu-tray" <<EOF
#!/bin/sh
export NINAIVU_HOME="$state"
exec "$prefix/python/bin/python3" -m ninaivu.desktop.tray "\$@"
EOF
cat > "$prefix/ninaivu-panel" <<EOF
#!/bin/sh
export NINAIVU_HOME="$state"
exec "$prefix/python/bin/python3" -m ninaivu.desktop.app "\$@"
EOF
cat > "$prefix/uninstall" <<EOF
#!/bin/sh
# Remove Ninaivu's program, command, desktop entry and service. The library is
# never touched; the index and settings under $state are kept unless --purge.
systemctl --user disable --now ninaivu 2>/dev/null || true
systemctl disable --now ninaivu 2>/dev/null || true
rm -f "$bindir/ninaivu" "$bindir/ninaivu-tray" "$bindir/ninaivu-panel" "$apps/ninaivu.desktop" "\$HOME/Desktop/ninaivu.desktop" \
      "\${XDG_CONFIG_HOME:-\$HOME/.config}/systemd/user/ninaivu.service" /etc/systemd/system/ninaivu.service
[ "\$1" = "--purge" ] && rm -rf "$state"
rm -rf "$prefix"
echo "Ninaivu removed."
EOF
chmod +x "$prefix/ninaivu" "$prefix/ninaivu-tray" "$prefix/ninaivu-panel" "$prefix/uninstall"
ln -sf "$prefix/ninaivu" "$bindir/ninaivu"
ln -sf "$prefix/ninaivu-tray" "$bindir/ninaivu-tray"
ln -sf "$prefix/ninaivu-panel" "$bindir/ninaivu-panel"

# The Control Panel in the applications menu and on the Desktop, for the
# machines that have one: it is what the person who runs the house opens, to
# start and stop Ninaivu and see how it is doing.
mkdir -p "$apps"
cat > "$apps/ninaivu.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Ninaivu Control Panel
Comment=Start and stop Ninaivu, and see how it is doing
Exec=$prefix/ninaivu-panel
Icon=folder-pictures
Terminal=false
Categories=Graphics;Photography;
EOF
if [ "$(id -u)" != 0 ] && [ -d "$HOME/Desktop" ]; then
    cp "$apps/ninaivu.desktop" "$HOME/Desktop/ninaivu.desktop"
    chmod +x "$HOME/Desktop/ninaivu.desktop"
    command -v gio >/dev/null 2>&1 && gio set "$HOME/Desktop/ninaivu.desktop" metadata::trusted true 2>/dev/null || true
fi

# The photographs folder, asked for once when there is somebody to ask.
if [ -z "$photos" ]; then
    photos="$HOME/Pictures"
    if [ -t 0 ] && [ "$quiet" = 0 ]; then
        printf 'Where are the photographs? [%s] ' "$photos"
        read -r answer
        [ -n "$answer" ] && photos=$answer
    fi
fi
photos=$(cd "$photos" 2>/dev/null && pwd || echo "$photos")
mkdir -p "$photos"

# The service: Ninaivu at boot, restarted if it stops.
if [ "$service" = 1 ] && command -v systemctl >/dev/null 2>&1; then
    if [ "$(id -u)" = 0 ]; then
        unit=/etc/systemd/system/ninaivu.service; scope=""
    else
        unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/ninaivu.service"; scope="--user"
    fi
    mkdir -p "$(dirname "$unit")"
    cat > "$unit" <<EOF
[Unit]
Description=Ninaivu — the family's photographs, at home
After=network-online.target

[Service]
Environment=NINAIVU_HOME=$state
ExecStart=$prefix/python/bin/python3 -m ninaivu "$photos" --supervised
Restart=on-failure
RestartSec=5

[Install]
WantedBy=$([ -n "$scope" ] && echo default.target || echo multi-user.target)
EOF
    if systemctl $scope daemon-reload 2>/dev/null && systemctl $scope enable --now ninaivu 2>/dev/null; then
        say "Started as a service; it starts again at boot."
        if [ -n "$scope" ] && command -v loginctl >/dev/null 2>&1; then
            loginctl enable-linger "$(id -un)" 2>/dev/null && say "It keeps running when you sign out." || true
        fi
    else
        say "Could not set up the service (no systemd session?). Start it with: ninaivu \"$photos\""
    fi
else
    say "Start it with: ninaivu \"$photos\""
fi

say ""
say "Ninaivu $version is installed."
say "  Open it:        http://$(hostname 2>/dev/null || echo localhost):8080  (the exact address is in the log a moment after it starts)"
say "  Control Panel:  ninaivu-panel, in the applications menu and on the Desktop"
say "  Command:        ninaivu <photos folder>     ($bindir is on PATH for most shells)"
say "  Log and state:  $state"
say "  Remove it:      $prefix/uninstall"
