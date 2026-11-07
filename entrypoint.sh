#!/bin/sh
# Container entrypoint.
#
# A bind-mounted ./data directory is created by Docker as root-owned on first
# run, which breaks the unprivileged app user. So: create it, hand it to the
# app user, then drop privileges. Root exists for those two steps only.
set -e

APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"
DATA_DIR="$(dirname "${DB_PATH:-/data/baby.db}")"

mkdir -p "$DATA_DIR"

if [ "$(id -u)" = "0" ]; then
    chown -R "$APP_UID:$APP_GID" "$DATA_DIR"
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups \
        uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
fi

exec uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
