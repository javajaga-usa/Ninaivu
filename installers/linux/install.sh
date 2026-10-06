#!/bin/sh
# Install Ninaivu from an unpacked payload (header.sh unpacks it and calls this).
#
#   install.sh PAYLOAD [--prefix DIR] [--photos DIR] [--no-service] [--quiet]
#
# What it does, in order: copies the private Python and installs every bundled
# wheel into it, offline; makes the `ninaivu` command (and `ninaivu-tray` for a
# desktop); writes a desktop entry; sets up a systemd service that starts
# Ninaivu at boot (a user service, or, when run as root, a system one that
# runs as its own user, ninaivu) pointed at the photographs folder; starts it;
# says where to open it. Run again to upgrade in place: the photographs folder
# the service was given, the settings, the index and the AI models are kept,
# because they live outside the program.
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
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
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
# Reading back what an earlier install wrote into the service: the reverse of
# systemd_quote (for a word of ExecStart=) and of systemd_env.
systemd_unquote() { sed -e 's/%%/%/g' -e 's/\$\$/$/g' -e 's/\\\(.\)/\1/g'; }
systemd_unenv() { sed -e 's/%%/%/g' -e 's/\\\(.\)/\1/g'; }
# A state folder the server has used: its index or its settings are there.
has_data() { [ -f "$1/index.db" ] || [ -f "$1/config.json" ]; }

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

# An earlier install: what its service was pointed at is kept. Re-running the
# installer to upgrade used to point the service at ~/Pictures again, and the
# server took that as the library from then on.
old_photos=""; old_server_state=""; old_user=""; had_unit=0
if [ -f "$unit" ]; then
    had_unit=1
    old_photos=$(sed -n 's/^ExecStart=.* -m ninaivu "\(.*\)" --supervised$/\1/p' "$unit" | systemd_unquote)
    old_server_state=$(sed -n 's/^Environment="NINAIVU_STATE_DIR=\(.*\)"$/\1/p' "$unit" | systemd_unenv)
    old_user=$(sed -n 's/^User=//p' "$unit")
fi

# Where the server keeps the index, the accounts, the keys and the cloud
# sign-ins. The server reads NINAIVU_STATE_DIR, not NINAIVU_HOME, so an install
# that set only NINAIVU_HOME had the server in ~/.ninaivu (root's, for the
# system service) while the installer named another folder and --purge left
# the data behind. That folder, when it has data, is kept; otherwise it is
# the state folder here.
if [ "$(id -u)" = 0 ]; then
    legacy="$(getent passwd root 2>/dev/null | cut -d: -f6)"
    legacy="${legacy:-/root}/.ninaivu"
else
    legacy="$HOME/.ninaivu"
    if [ -n "${XDG_DATA_HOME:-}" ] && ! has_data "$legacy" && has_data "$XDG_DATA_HOME/ninaivu"; then
        legacy="$XDG_DATA_HOME/ninaivu"
    fi
fi
if [ -n "$old_server_state" ]; then
    server_state=$old_server_state
elif has_data "$legacy"; then
    server_state=$legacy
else
    server_state=$state
fi
no_newline state "$server_state"
# The AI models, outside the program: an upgrade replaces the program whole.
models="$state/ai-models"

# The system service runs as its own user, ninaivu, not as root: a fault in
# reading an image or a video would otherwise be root's. It may still use
# port 80 (CAP_NET_BIND_SERVICE). An earlier install that ran as root with its
# data in root's home stays as it was; docs/admin-guide.md says how to move it.
service_user=""
if [ "$(id -u)" = 0 ]; then
    if [ -n "$old_user" ]; then
        service_user=$old_user
    elif [ "$server_state" = "$legacy" ]; then
        service_user=""                 # root's data, in root's home
    else
        service_user=ninaivu
        server_state=$state
    fi
    # No service, nothing runs as that user.
    if [ "$service" != 1 ] || [ "$have_systemd" != 1 ]; then service_user=""; fi
fi
if [ "$service_user" = ninaivu ] && ! id ninaivu >/dev/null 2>&1; then
    if command -v useradd >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "$state" --no-create-home --shell /usr/sbin/nologin ninaivu
    elif command -v adduser >/dev/null 2>&1; then
        # Debian's adduser, or BusyBox's (Alpine), which takes other options.
        adduser --system --group --home "$state" --no-create-home --disabled-login ninaivu 2>/dev/null \
            || { addgroup -S ninaivu && adduser -S -D -H -h "$state" -s /sbin/nologin -G ninaivu ninaivu; }
    else
        echo "Could not make the user the service runs as (no useradd or adduser); nothing was changed." >&2
        exit 1
    fi
fi
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

# The AI models an earlier version downloaded went into its own Python's
# site-packages, which the upgrade below deletes: several gigabytes, fetched
# again one by one. They are moved out first; one that cannot be leaves
# everything as it was.
for old_models in "$prefix"/python/lib/python3*/site-packages/.ai-models; do
    [ -d "$old_models" ] || continue
    say "Moving the downloaded AI models to $models…"
    mkdir -p "$models"
    for item in "$old_models"/* "$old_models"/.[!.]* "$old_models"/..?*; do
        [ -e "$item" ] || [ -L "$item" ] || continue
        [ -e "$models/${item##*/}" ] && continue        # already there
        if ! mv "$item" "$models/"; then
            echo "Could not move the AI models from $old_models to $models; nothing was changed." >&2
            rm -rf "$prefix/python.new"
            if [ "$was_active" = 1 ]; then systemctl $scope start ninaivu 2>/dev/null || true; fi
            exit 1
        fi
    done
done

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

# The commands. NINAIVU_HOME is where the Control Panel keeps its run-time
# files and the server log, as the Windows and Mac installers set it;
# NINAIVU_STATE_DIR is where the server keeps the index and the settings, and
# NINAIVU_AI_MODELS_DIR the models. Paths are written as quoted shell words,
# so a space, a quote or a $ in a folder's name is taken as it is.
q_state=$(sh_quote "$state")
q_server_state=$(sh_quote "$server_state")
q_models=$(sh_quote "$models")
q_python=$(sh_quote "$prefix/python/bin/python3")
for command in "ninaivu:ninaivu" "ninaivu-tray:ninaivu.desktop.tray" "ninaivu-panel:ninaivu.desktop.app"; do
    cat > "$prefix/${command%%:*}" <<EOF
#!/bin/sh
export NINAIVU_HOME=$q_state
export NINAIVU_STATE_DIR=$q_server_state
export NINAIVU_AI_MODELS_DIR=$q_models
exec $q_python -m ${command#*:} "\$@"
EOF
done
# Only what this installer put there is removed: --prefix may have been a
# folder with other things in it (~/Apps, or the library itself), and the
# whole of it used to go.
q_prefix=$(sh_quote "$prefix")
cat > "$prefix/uninstall" <<EOF
#!/bin/sh
# Remove Ninaivu's program, commands, desktop entry and service. The library is
# never touched; the index, the settings and the AI models are kept unless
# --purge.
prefix=$q_prefix
systemctl $scope disable --now ninaivu 2>/dev/null || true
rm -f $(sh_quote "$unit")
systemctl $scope daemon-reload 2>/dev/null || true
for name in ninaivu ninaivu-tray ninaivu-panel; do
    link=$(sh_quote "$bindir")/"\$name"
    if [ -L "\$link" ] && [ "\$(readlink "\$link")" = "\$prefix/\$name" ]; then rm -f "\$link"; fi
done
rm -f $(sh_quote "$apps/ninaivu.desktop")
$(if [ "$(id -u)" != 0 ]; then printf '%s' 'rm -f "$HOME/Desktop/ninaivu.desktop"'; fi)
rm -rf "\$prefix/python" "\$prefix/python.new" "\$prefix/python.old"
rm -f "\$prefix/ninaivu" "\$prefix/ninaivu-tray" "\$prefix/ninaivu-panel" \\
      "\$prefix/LICENSE" "\$prefix/README.md" "\$prefix/VERSION" "\$prefix/uninstall"
rmdir "\$prefix" 2>/dev/null || echo "\$prefix has other files in it, so it was left."
if [ "\$1" = "--purge" ]; then
    for folder in $q_server_state $q_models $q_state; do
        case "\$folder" in /|"\$HOME"|"\$prefix") continue ;; esac
        rm -rf "\$folder"
    done
fi
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

# The photographs folder: the one an earlier install's service was given,
# or, when there is none but the server already has a library, none at all
# (the server opens the library it has); otherwise asked for once when there
# is somebody to ask.
keep_library=0
if [ -z "$photos" ]; then
    if [ "$had_unit" = 1 ]; then
        photos=$old_photos
        [ -n "$photos" ] || keep_library=1
    elif has_data "$server_state"; then
        keep_library=1
    fi
fi
if [ -z "$photos" ] && [ "$keep_library" = 0 ]; then
    if [ "$service_user" = ninaivu ]; then photos="$state/photos"; else photos="$HOME/Pictures"; fi
    if [ -t 0 ] && [ "$quiet" = 0 ]; then
        printf 'Where are the photographs? [%s] ' "$photos"
        read -r answer
        [ -n "$answer" ] && photos=$answer
    fi
fi
library_word=""
if [ -n "$photos" ]; then
    photos=$(cd "$photos" 2>/dev/null && pwd || echo "$photos")
    no_newline photographs "$photos"
    made_photos=0
    [ -d "$photos" ] || made_photos=1
    mkdir -p "$photos"
    library_word=" $(systemd_quote "$photos")"
fi

# The service's own user owns its state, and a photographs folder made for it.
# A folder that was already there is not changed: it is said what to do.
if [ "$service_user" = ninaivu ]; then
    mkdir -p "$state" "$models"
    chown -R ninaivu:ninaivu "$state"
    if [ -n "$photos" ]; then
        [ "$made_photos" = 1 ] && chown ninaivu:ninaivu "$photos"
        if command -v runuser >/dev/null 2>&1 \
                && ! runuser -u ninaivu -- sh -c 'test -r "$1" && test -w "$1" && test -x "$1"' sh "$photos"; then
            say "The service runs as the user ninaivu, which cannot use $photos yet."
            say "  Give it access, for example: sudo setfacl -R -m u:ninaivu:rwX -m d:u:ninaivu:rwX \"$photos\""
        fi
    fi
fi

# The service: Ninaivu at boot, restarted if it stops.
if [ "$service" = 1 ] && [ "$have_systemd" = 1 ]; then
    mkdir -p "$(dirname "$unit")"
    cat > "$unit" <<EOF
[Unit]
Description=Ninaivu — the family's photographs, at home
After=network-online.target

[Service]
$(if [ -n "$service_user" ]; then
    printf 'User=%s\nGroup=%s\n' "$service_user" "$service_user"
    printf 'AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE\n'
fi)
Environment=$(systemd_env NINAIVU_HOME "$state")
Environment=$(systemd_env NINAIVU_STATE_DIR "$server_state")
Environment=$(systemd_env NINAIVU_AI_MODELS_DIR "$models")
ExecStart=/usr/bin/env $(systemd_quote "$prefix/python/bin/python3") -m ninaivu$library_word --supervised
Restart=on-failure
RestartSec=5
$(if [ -z "$scope" ]; then cat <<'HARDENING'
# What installers/systemd/ninaivu.service has, less what would stop a library
# or a backup on another disk being added later (ProtectSystem=strict,
# ProtectHome, PrivateDevices).
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
HARDENING
fi)

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
        say "Could not set up the service (no systemd session?). Start it with: ninaivu${photos:+ \"$photos\"}"
    fi
else
    if [ "$was_active" = 1 ]; then
        systemctl $scope start ninaivu 2>/dev/null && say "Started the service again." || true
    fi
    say "Start it with: ninaivu${photos:+ \"$photos\"}"
fi
if [ "$(id -u)" = 0 ] && [ "$service" = 1 ] && [ "$have_systemd" = 1 ] && [ -z "$service_user" ]; then
    say "The service still runs as root, because its data is in $server_state."
    say "  To run it as its own user, see \"The Linux system service\" in the technical administrator guide."
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
[ "$server_state" = "$state" ] || say "  Index and settings: $server_state"
say "  AI models:      $models"
say "  Remove it:      $prefix/uninstall"
