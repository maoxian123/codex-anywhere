#!/bin/sh
set -eu
codex-anywhere db-upgrade
exec "$@"
