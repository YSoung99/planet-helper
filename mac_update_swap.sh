#!/bin/sh
# Wait for the controller to save and close before exchanging application bundles.
set -eu
pid=$1
app=$2
candidate=$3
backup=$4
stage=$5
data=$6
# Versioned bundle names: the new app may live at a different path than the running one.
target=${7:-$app}

restart() {
    /usr/bin/open -n --env "PLANET_EXP_DATA_DIR=$data" "$1"
}

attempt=0
while kill -0 "$pid" 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 150 ]; then
        /bin/rm -rf "$stage"
        exit 1
    fi
    /bin/sleep 0.2
done

restore() {
    if [ -d "$stage/current.app" ]; then
        if [ -d "$target" ]; then /bin/mv "$target" "$stage/failed.app"; fi
        if [ -d "$app" ]; then /bin/mv "$app" "$stage/failed-current.app"; fi
        /bin/mv "$stage/current.app" "$app"
    fi
    if [ -d "$stage/previous.app" ]; then /bin/mv "$stage/previous.app" "$backup"; fi
    restart "$app"
}
# A pinned Dock icon remembers the bundle itself, so it follows the old app into the backup slot.
repoint_dock() {
    prefs=$stage/dock.plist
    buddy=/usr/libexec/PlistBuddy
    bundle=$("$buddy" -c 'Print :CFBundleIdentifier' "$target/Contents/Info.plist") || return 0
    url=$(/usr/bin/osascript -l JavaScript -e 'function run(argv) { return $.NSURL.fileURLWithPath(argv[0]).absoluteString.js }' "$target") || return 0
    label=$(/usr/bin/basename "$target" .app)
    /usr/bin/defaults export com.apple.dock "$prefs" || return 0
    changed=
    i=0
    while "$buddy" -c "Print :persistent-apps:$i" "$prefs" >/dev/null 2>&1; do
        tile=:persistent-apps:$i:tile-data
        if [ "$("$buddy" -c "Print $tile:bundle-identifier" "$prefs" 2>/dev/null)" = "$bundle" ]; then
            "$buddy" -c "Set $tile:file-data:_CFURLString $url" -c "Set $tile:file-label $label" "$prefs"
            "$buddy" -c "Delete $tile:book" "$prefs" 2>/dev/null || true
            changed=1
        fi
        i=$((i + 1))
    done
    if [ -n "$changed" ]; then
        /usr/bin/defaults import com.apple.dock "$prefs" && /usr/bin/killall Dock
    fi
}

trap restore EXIT
if [ -d "$backup" ]; then /bin/mv "$backup" "$stage/previous.app"; fi
/bin/mv "$app" "$stage/current.app"
/bin/mv "$candidate" "$target"
restart "$target"
/bin/mv "$stage/current.app" "$backup"
trap - EXIT
repoint_dock || true
/bin/rm -rf "$stage"
