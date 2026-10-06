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

# Each file below reads a path its own way, and a custom --prefix or a home
# folder with a space in it ("My Apps") was split at the space by the desktop
# entry and the service: the Control Panel and the server failed to start
# after an install that said it had worked. One quoting rule for each.
#   sh_quote: a shell word, in single quotes.
sh_quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
#   desktop_quote: one argument of a desktop entry's Exec= (the Desktop Entry
#   Specification: in double quotes, with \ " ` $ escaped; % doubled; then the
#   value's own string escaping, which doubles every backslash again).
desktop_quote() {
    printf '"%s"' "$(printf '%s' "$1" | sed -e 's/[\\"`$]/\\&/g')" | sed -e 's/%/%%/g' -e 's/\\/\\\\/g'
}
#   systemd_quote: one word of ExecStart= (in double quotes with \ and "
#   escaped; % specifiers and $ variables doubled so they are taken as text).
systemd_quote() {
    printf '"%s"' "$(printf '%s' "$1" | sed -e 's/[\\"]/\\&/g' -e 's/%/%%/g' -e 's/\$/$$/g')"
}
#   systemd_env: a whole Environment= assignment, quoted the same way ($ is
#   not expanded there, so it stays as it is).
systemd_env() {
    printf '"%s=%s"' "$1" "$(printf '%s' "$2" | sed -e 's/[\\"]/\\&/g' -e 's/%/%%/g')"
}
# None of the three can carry a line break in a path.
no_newline() {
    case "$2" in
        *"
"*) echo "The $1 folder may not have a line break in its name." >&2; exit 2 ;;
    esac
}

if [ "$(id -u)" = 0 ]; then
    default_prefix=/opt/ninaivu; bindir=/usr/local/bin; apps=/usr/local/share/applications; state=/var/lib/ninaivu
    unit=/etc/systemd/system/ninaivu.service; scope=""
else
    default_prefix="${XDG_DATA_HOME:-$HOME/.local/share}/ninaivu"; bindir="$HOME/.local/bin"
    apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"; state="${XDG_STATE_HOME:-$HOME/.local/state}/ninaivu"
    unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/ninaivu.service"; scope="--user"
fi
prefix=${prefix:-$default_prefix}
no_newline program "$prefix"
no_newline state "$state"
have_systemd=0
command -v systemctl >/dev/null 2>&1 && have_systemd=1
say "Ninaivu $version → $prefix"

# The machine's own Python is not used, and it need not have one.
mkdir -p "$prefix" "$bindir" "$state"
rm -rf "$prefix/python.new"
cp -R "$payload/python" "$prefix/python.new"
py="$prefix/python.new/bin/python3"
"$py" -m pip install --quiet --no-index --no-deps --no-warn-script-location "$payload"/wheels/*.whl

# An upgrade: a Ninaivu still running from the old Python is stopped before
# its files are replaced under it, and started again at the end. Replacing
# them while it ran left the old server going after "installed", importing
# whatever it had not loaded yet from the new version's files.
was_active=0
if [ "$have_systemd" = 1 ] && systemctl $scope is-active --quiet ninaivu 2>/dev/null; then
    say "Stopping the running Ninaivu for the upgrade…"
    systemctl $scope stop ninaivu 2>/dev/null || true
    was_active=1
fi
# pgrep reads a pattern, so the path's own . + ( and the like are escaped.
old=$(printf '%s' "$prefix/python/bin/python3" | sed -e 's/[][\\.*^$+?(){}|]/\\&/g')
if [ -x "$prefix/python/bin/python3" ] && command -v pgrep >/dev/null 2>&1 && pgrep -f -- "$old" >/dev/null 2>&1; then
    # Started by hand, or the tray and the Control Panel: asked to close.
    say "Closing Ninaivu programs still running from the old version…"
    pkill -TERM -f -- "$old" 2>/dev/null || true
    waited=0
    while pgrep -f -- "$old" >/dev/null 2>&1 && [ "$waited" -lt 30 ]; do
        sleep 1; waited=$((waited + 1))
    done
    if pgrep -f -- "$old" >/dev/null 2>&1; then
        echo "Ninaivu is still running from $prefix and would not close; nothing was changed." >&2
        echo "Close it and run the installer again." >&2
        rm -rf "$prefix/python.new"
        if [ "$was_active" = 1 ]; then systemctl $scope start ninaivu 2>/dev/null || true; fi
        exit 1
    fi
fi

# Only now is the old one replaced, so a failure above leaves a working
# install. It is kept beside the new one until the new one is in place.
rm -rf "$prefix/python.old"
if [ -d "$prefix/python" ]; then mv "$prefix/python" "$prefix/python.old"; fi
if ! mv "$prefix/python.new" "$prefix/python"; then
    if [ -d "$prefix/python.old" ]; then mv "$prefix/python.old" "$prefix/python"; fi
    echo "Could not put the new version in place; the old one is kept." >&2
    if [ "$was_active" = 1 ]; then systemctl $scope start ninaivu 2>/dev/null || true; fi
    exit 1
fi
rm -rf "$prefix/python.old"
cp "$payload/LICENSE" "$payload/README.md" "$prefix/"
printf '%s\n' "$version" > "$prefix/VERSION"

# The commands. NINAIVU_HOME is where the server keeps its state, its log and
# its run file, as the Windows and Mac installers set it. Paths are written
# as quoted shell words, so a space, a quote or a $ in a folder's name is
# taken as it is.
q_state=$(sh_quote "$state")
q_python=$(sh_quote "$prefix/python/bin/python3")
cat > "$prefix/ninaivu" <<EOF
#!/bin/sh
export NINAIVU_HOME=$q_state
exec $q_python -m ninaivu "\$@"
EOF
cat > "$prefix/ninaivu-tray" <<EOF
#!/bin/sh
export NINAIVU_HOME=$q_state
exec $q_python -m ninaivu.desktop.tray "\$@"
EOF
cat > "$prefix/ninaivu-panel" <<EOF
#!/bin/sh
export NINAIVU_HOME=$q_state
exec $q_python -m ninaivu.desktop.app "\$@"
EOF
cat > "$prefix/uninstall" <<EOF
#!/bin/sh
# Remove Ninaivu's program, command, desktop entry and service. The library is
# never touched; the index and settings in the state folder are kept unless
# --purge.
systemctl --user disable --now ninaivu 2>/dev/null || true
systemctl disable --now ninaivu 2>/dev/null || true
rm -f $(sh_quote "$bindir/ninaivu") $(sh_quote "$bindir/ninaivu-tray") $(sh_quote "$bindir/ninaivu-panel") \\
      $(sh_quote "$apps/ninaivu.desktop") "\$HOME/Desktop/ninaivu.desktop" \\
      "\${XDG_CONFIG_HOME:-\$HOME/.config}/systemd/user/ninaivu.service" /etc/systemd/system/ninaivu.service
[ "\$1" = "--purge" ] && rm -rf $q_state
rm -rf $(sh_quote "$prefix")
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
Exec=$(desktop_quote "$prefix/ninaivu-panel")
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
no_newline photographs "$photos"
mkdir -p "$photos"

# The service: Ninaivu at boot, restarted if it stops.
if [ "$service" = 1 ] && [ "$have_systemd" = 1 ]; then
    mkdir -p "$(dirname "$unit")"
    cat > "$unit" <<EOF
[Unit]
Description=Ninaivu — the family's photographs, at home
After=network-online.target

[Service]
Environment=$(systemd_env NINAIVU_HOME "$state")
ExecStart=$(systemd_quote "$prefix/python/bin/python3") -m ninaivu $(systemd_quote "$photos") --supervised
Restart=on-failure
RestartSec=5

[Install]
WantedBy=$([ -n "$scope" ] && echo default.target || echo multi-user.target)
EOF
    # restart rather than enable --now: --now starts a stopped service but
    # leaves a running one as it is, which on an upgrade was the old version.
    if systemctl $scope daemon-reload 2>/dev/null && systemctl $scope enable ninaivu 2>/dev/null \
            && systemctl $scope restart ninaivu 2>/dev/null; then
        say "Started as a service; it starts again at boot."
        if [ -n "$scope" ] && command -v loginctl >/dev/null 2>&1; then
            loginctl enable-linger "$(id -un)" 2>/dev/null && say "It keeps running when you sign out." || true
        fi
    else
        say "Could not set up the service (no systemd session?). Start it with: ninaivu \"$photos\""
    fi
else
    if [ "$was_active" = 1 ]; then
        systemctl $scope start ninaivu 2>/dev/null && say "Started the service again." || true
    fi
    say "Start it with: ninaivu \"$photos\""
fi

# Port 80 for the system service. An ordinary user may not use it, and the
# server then takes 8080 (UNPRIVILEGED_FALLBACK in ninaivu/__main__.py), or
# the next free port after that.
if [ "$(id -u)" = 0 ]; then port_text=""; else port_text=":8080"; fi
say ""
say "Ninaivu $version is installed."
say "  Open it:        http://$(hostname 2>/dev/null || echo localhost)$port_text  (the exact address is in the log a moment after it starts)"
say "  Control Panel:  ninaivu-panel, in the applications menu and on the Desktop"
say "  Command:        ninaivu <photos folder>     ($bindir is on PATH for most shells)"
say "  Log and state:  $state"
say "  Remove it:      $prefix/uninstall"
