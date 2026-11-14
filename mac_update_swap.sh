#!/bin/sh
# Wait for the controller to save and close before exchanging application bundles.
set -eu
pid=$1
app=$2
candidate=$3
backup=$4
stage=$5
data=$6

restart() {
    /usr/bin/open -n --env "PLANET_EXP_DATA_DIR=$data" "$app"
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
        if [ -d "$app" ]; then /bin/mv "$app" "$stage/failed.app"; fi
        /bin/mv "$stage/current.app" "$app"
    fi
    if [ -d "$stage/previous.app" ]; then /bin/mv "$stage/previous.app" "$backup"; fi
    restart
}
trap restore EXIT
if [ -d "$backup" ]; then /bin/mv "$backup" "$stage/previous.app"; fi
/bin/mv "$app" "$stage/current.app"
/bin/mv "$candidate" "$app"
restart
/bin/mv "$stage/current.app" "$backup"
trap - EXIT
/bin/rm -rf "$stage"
