#!/bin/sh
set -eu
cd /opt/frontdoor
exec .venv/bin/python -m app.main --data-dir /data
