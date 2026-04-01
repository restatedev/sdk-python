#!/usr/bin/env sh

PORT=${PORT:-"9080"}

if [ -n "$MAX_CONCURRENT_STREAMS" ]; then
    # respect the MAX_CONCURRENT_STREAMS environment variable
    awk 'BEGIN { FS=OFS="=" } $1=="h2_max_concurrent_streams " {$2=" '$MAX_CONCURRENT_STREAMS'"} {print}' hypercorn-config.toml > hypercorn-config.toml.new
    mv hypercorn-config.toml.new hypercorn-config.toml
fi

if [ -n "$RESTATE_LOGGING" ]; then
    # unification of the RESTATE_LOGGING environment variable
    # which is also used by the node-test-services.
    #
    # Set by the e2e-verification-runner
    export RESTATE_CORE_LOG=$RESTATE_LOGGING
fi

python3 -m hypercorn testservices:app --config hypercorn-config.toml --bind "0.0.0.0:${PORT}"
