#!/usr/bin/env bash
set -e
cd -- "$(dirname -- "$0")"

ledger_args=(--db data/live-futures-demo.db)
for arg in "$@"; do
    case "$arg" in
        --db|--db=*|--no-ledger) ledger_args=(); break ;;
    esac
done

exec uv run python -m deploy.portfolio_monitor \
    --bootstrap-servers localhost:9094 \
    --config data/config.futures-demo.json \
    "${ledger_args[@]}" "$@"
