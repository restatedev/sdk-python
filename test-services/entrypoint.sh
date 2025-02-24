#!/usr/bin/env sh

PORT=${PORT:-"9080"}

python3 -m hypercorn testservices:app --config hypercorn-config.toml --bind "0.0.0.0:${PORT}"
