#!/usr/bin/env bash
# Integration test for the ceph stack: bootstrap a single-node cluster and
# verify HEALTH_OK plus a live dashboard API endpoint.
set -euo pipefail

STACK=ceph
CONTAINER=ceph_node
CEPH_CONF=/var/lib/ceph-devstack/cluster/ceph.conf
DASHBOARD_PORT=8080
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=admin

cd "$(dirname "$0")/../.."

cleanup() {
    local status=$?
    if [[ $status -ne 0 ]]; then
        echo "=== ${CONTAINER} logs (last 200 lines) ===" >&2
        podman logs "$CONTAINER" 2>&1 | tail -200 >&2 || true
    fi
    uv run ceph-devstack --stack "$STACK" -v stop || true
    uv run ceph-devstack --stack "$STACK" -v remove || true
    exit "$status"
}
trap cleanup EXIT

uv run ceph-devstack --stack "$STACK" -v doctor --fix
uv run ceph-devstack --stack "$STACK" -v pull
uv run ceph-devstack --stack "$STACK" -v create
uv run ceph-devstack --stack "$STACK" -v start

health=""
for _ in $(seq 1 90); do
    health="$(
        podman exec "$CONTAINER" ceph --conf "$CEPH_CONF" health 2>/dev/null \
            | awk '{print $1}' || true
    )"
    if [[ "$health" == "HEALTH_OK" ]]; then
        echo "Cluster reached HEALTH_OK"
        break
    fi
    sleep 2
done

if [[ "$health" != "HEALTH_OK" ]]; then
    echo "Expected HEALTH_OK, got: ${health:-<empty>}" >&2
    exit 1
fi

dashboard_ok=false
for _ in $(seq 1 30); do
    if curl -sf -X POST "http://127.0.0.1:${DASHBOARD_PORT}/api/auth" \
        -H 'Accept: application/vnd.ceph.api.v1.0+json' \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"${DASHBOARD_USER}\",\"password\":\"${DASHBOARD_PASSWORD}\"}" \
        >/dev/null; then
        dashboard_ok=true
        break
    fi
    sleep 2
done

if [[ "$dashboard_ok" != "true" ]]; then
    echo "Dashboard API did not become ready on port ${DASHBOARD_PORT}" >&2
    exit 1
fi

echo "Dashboard API is live on port ${DASHBOARD_PORT}"
echo "Ceph stack integration test passed"
