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
trap restore EXIT
if [ -d "$backup" ]; then /bin/mv "$backup" "$stage/previous.app"; fi
/bin/mv "$app" "$stage/current.app"
/bin/mv "$candidate" "$target"
restart "$target"
/bin/mv "$stage/current.app" "$backup"
trap - EXIT
/bin/rm -rf "$stage"
