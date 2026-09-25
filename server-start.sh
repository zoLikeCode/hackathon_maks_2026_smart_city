#!/bin/sh
set -eu

cd "$(dirname "$0")"

docker compose -f compose.yaml -f compose.server.yaml up -d --build --remove-orphans
docker compose -f compose.yaml -f compose.server.yaml ps
