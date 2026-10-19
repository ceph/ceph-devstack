#!/bin/bash
# Bootstrap (on first start) and run a single-node mon/mgr/osd cluster.
# Every setting comes from the container environment; see CephNode.
set -euo pipefail

CLUSTER_DIR="${CLUSTER_DIR:?}"
CEPH_CONF="${CEPH_CONF:?}"
# Exported once the cluster is bootstrapped; until then ceph tools would fail
# to read a CEPH_CONF that does not exist yet.
export -n CEPH_CONF
OSD_DEVICES="${OSD_DEVICES?}"
DASHBOARD_ENABLED="${DASHBOARD_ENABLED:?}"
DASHBOARD_PORT="${DASHBOARD_PORT:?}"
DASHBOARD_SSL="${DASHBOARD_SSL:?}"
DASHBOARD_USER="${DASHBOARD_USER:?}"
DASHBOARD_PASSWORD_FILE="${DASHBOARD_PASSWORD_FILE:?}"
DASHBOARD_SHOW_PASSWORD="${DASHBOARD_SHOW_PASSWORD:?}"
CONTAINER_NAME="${CONTAINER_NAME:?}"

MON_ID=a
MGR_ID=x
ADMIN_KEYRING="${CLUSTER_DIR}/ceph.client.admin.keyring"
BOOTSTRAP_DIR="${CLUSTER_DIR}/bootstrap"
BOOTSTRAP_OSD_KEYRING=/var/lib/ceph/bootstrap-osd/ceph.keyring

osd_devices=()
osd_ids=()      # filled by start_raw_osd
IFS=',' read -ra listed_devices <<< "${OSD_DEVICES}"
for dev in "${listed_devices[@]}"; do
    if [[ -b "${dev}" ]]; then
        osd_devices+=("${dev}")
    fi
done

ceph_cmd() { ceph --conf "${CEPH_CONF}" "$@"; }

retry() {
    local max_attempts="$1" sleep_secs="$2" fail_msg="$3"
    shift 3
    for _ in $(seq 1 "${max_attempts}"); do
        if "$@"; then
            return 0
        fi
        sleep "${sleep_secs}"
    done
    echo "${fail_msg}" >&2
    return 1
}

json_field() {
    local key="$1" default="${2:-}"
    python3 -c "import json,sys; print(json.load(sys.stdin).get('${key}', '${default}'))" \
        2>/dev/null || echo "${default}"
}

# $1 is a regex; end it with "( |$)" so that e.g. osd 1 doesn't match osd 10.
is_running() { pgrep -f "$1" >/dev/null 2>&1; }

detect_mon_ip() {
    local ip

    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | \
        awk '{for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit }}')"
    if [[ -n "${ip}" ]]; then
        echo "${ip}"
        return
    fi
    ip="$(hostname -I | awk '{print $1}')"
    if [[ -n "${ip}" ]]; then
        echo "${ip}"
        return
    fi
    echo "127.0.0.1"
}

detect_public_network() {
    local mon_ip="$1"
    local net

    net="$(ip route list 2>/dev/null | grep -w "${mon_ip}" | grep -v default | \
        grep -E '/[0-9]+' | awk '{print $1; exit}')"
    if [[ -n "${net}" ]]; then
        echo "${net}"
        return
    fi
    echo "${mon_ip}/32"
}

# ceph.conf marks a finished bootstrap, so it is written to ceph.conf.new and
# moved into place last; a failed bootstrap is redone from scratch.
bootstrap_cluster() {
    local fsid mon_ip public_network mon_keyring monmap mon_host conf
    fsid="$(uuidgen)"
    mon_ip="$(detect_mon_ip)"
    public_network="$(detect_public_network "${mon_ip}")"
    mon_host="v2:${mon_ip}:3300,v1:${mon_ip}:6789"
    mon_keyring="${BOOTSTRAP_DIR}/ceph.mon.keyring"
    monmap="${BOOTSTRAP_DIR}/ceph.monmap"
    conf="${CEPH_CONF}.new"

    # A new cluster owns no OSDs yet. Block-pool regions are reused with only
    # their first 4 KiB zeroed, so a leftover BlueStore label would make
    # prepare_raw_osd skip mkfs and start an OSD with another cluster's fsid
    # and key. zap-device also clears the label copies newer releases keep
    # further in.
    local dev
    for dev in "${osd_devices[@]}"; do
        ceph-bluestore-tool zap-device --dev "${dev}" \
            --yes-i-really-really-mean-it >/dev/null 2>&1 || true
        dd if=/dev/zero of="${dev}" bs=1M count=1 conv=notrunc status=none
    done

    rm -rf "${BOOTSTRAP_DIR}" "/var/lib/ceph/mon/ceph-${MON_ID}"
    mkdir -p "${BOOTSTRAP_DIR}" "/var/lib/ceph/mon/ceph-${MON_ID}" \
        "$(dirname "${BOOTSTRAP_OSD_KEYRING}")"

    ceph-authtool --create-keyring "${mon_keyring}" --gen-key -n mon. --cap mon 'allow *'
    ceph-authtool "${mon_keyring}" --gen-key -n client.admin \
        --cap mon 'allow *' \
        --cap osd 'allow *' \
        --cap mds 'allow *' \
        --cap mgr 'allow *'
    ceph-authtool --create-keyring "${BOOTSTRAP_OSD_KEYRING}" \
        --gen-key -n client.bootstrap-osd \
        --cap mon 'profile bootstrap-osd'
    ceph-authtool "${mon_keyring}" --import-keyring "${BOOTSTRAP_OSD_KEYRING}"

    monmaptool --create --clobber --fsid "${fsid}" \
        --addv "${MON_ID}" "[${mon_host}]" \
        "${monmap}"

    cat > "${conf}" <<CONF
[global]
fsid = ${fsid}
mon initial members = ${MON_ID}
mon host = ${mon_host}
public addr = ${mon_ip}
cluster addr = ${mon_ip}
public network = ${public_network}
ms bind msgr2 = true
ms bind msgr1 = true
auth cluster required = cephx
auth service required = cephx
auth client required = cephx
auth allow insecure global id reclaim = false
# Three OSDs in the default layout, all on this host; one OSD may be out.
osd pool default size = 2
osd pool default min size = 1
osd crush chooseleaf type = 0
# Dev hosts often run with a nearly full disk, which holds the mon store too;
# the default (30%) would keep the cluster in HEALTH_WARN (MON_DISK_LOW).
mon data avail warn = 10

[client]
keyring = ${ADMIN_KEYRING}

[mon.${MON_ID}]
host = $(hostname -s 2>/dev/null || hostname)
public bind addr =

[osd]
osd numa auto affinity = false
CONF

    ceph-mon --mkfs -i "${MON_ID}" --monmap "${monmap}" --keyring "${mon_keyring}" \
        --conf "${conf}"

    ceph-authtool --create-keyring "${ADMIN_KEYRING}"
    ceph-authtool "${ADMIN_KEYRING}" --import-keyring "${mon_keyring}"

    chown -R ceph:ceph /var/lib/ceph
    chown ceph:ceph "${conf}" "${ADMIN_KEYRING}"
    rm -rf "${BOOTSTRAP_DIR}"
    mv "${conf}" "${CEPH_CONF}"
}

start_mon() {
    is_running "ceph-mon -i ${MON_ID}( |$)" && return
    ceph-mon -i "${MON_ID}" --conf "${CEPH_CONF}" --setuser ceph --setgroup ceph -f &
}

start_mgr() {
    local mgr_keyring="/var/lib/ceph/mgr/ceph-${MGR_ID}/keyring"
    is_running "ceph-mgr -i ${MGR_ID}( |$)" && return
    mkdir -p "$(dirname "${mgr_keyring}")"
    retry 30 2 "Failed to create mgr.${MGR_ID} keyring" \
        ceph_cmd auth get-or-create "mgr.${MGR_ID}" \
        mon 'allow profile mgr' osd 'allow *' mds 'allow *' \
        -o "${mgr_keyring}"
    chown -R ceph:ceph "$(dirname "${mgr_keyring}")"
    ceph-mgr -i "${MGR_ID}" --conf "${CEPH_CONF}" --no-mon-config \
        --keyring "${mgr_keyring}" \
        --setuser ceph --setgroup ceph -f &
}

_mgr_available() {
    local available
    available="$(ceph_cmd mgr stat -f json 2>/dev/null | json_field available False)"
    [[ "${available}" == "True" ]]
}

wait_for_mgr() {
    retry 60 2 "Timed out waiting for mgr" _mgr_available
}

setup_dashboard() {
    local mon_ip scheme port_option

    if [[ "${DASHBOARD_ENABLED}" != "true" ]]; then
        return 0
    fi

    mon_ip="$(detect_mon_ip)"

    ceph_cmd mgr module enable dashboard

    if [[ "${DASHBOARD_SSL}" == "true" ]]; then
        port_option="ssl_server_port"; scheme="https"
    else
        port_option="server_port"; scheme="http"
    fi
    ceph_cmd config set mgr mgr/dashboard/ssl "${DASHBOARD_SSL}" --force
    ceph_cmd config set mgr \
        "mgr/dashboard/${MGR_ID}/${port_option}" "${DASHBOARD_PORT}" --force
    if [[ "${DASHBOARD_SSL}" == "true" ]]; then
        ceph_cmd dashboard create-self-signed-cert 2>/dev/null || true
    fi
    ceph_cmd config set mgr \
        "mgr/dashboard/${MGR_ID}/server_addr" "0.0.0.0" --force

    # Not grep -q: exiting early closes the pipe, ceph -h dies of EPIPE, and
    # pipefail turns that into a spurious failure.
    _dashboard_cli_ready() { ceph_cmd -h 2>/dev/null | grep '^dashboard ' >/dev/null; }
    retry 60 2 "Timed out waiting for dashboard CLI" _dashboard_cli_ready

    if ceph_cmd dashboard ac-user-show "${DASHBOARD_USER}" >/dev/null 2>&1; then
        ceph_cmd dashboard ac-user-set-password "${DASHBOARD_USER}" \
            -i "${DASHBOARD_PASSWORD_FILE}" --force-password
    else
        ceph_cmd dashboard ac-user-create "${DASHBOARD_USER}" \
            -i "${DASHBOARD_PASSWORD_FILE}" administrator --force-password
    fi

    echo "Ceph dashboard: ${scheme}://${mon_ip}:${DASHBOARD_PORT}/"
    echo "  user: ${DASHBOARD_USER}"
    if [[ "${DASHBOARD_SHOW_PASSWORD}" == "true" ]]; then
        echo "  password: $(cat "${DASHBOARD_PASSWORD_FILE}")"
    else
        echo "  password: (set dashboard_show_password = true to log it)"
    fi
}

wait_for_mon() {
    _mon_ready() { ceph_cmd -s >/dev/null 2>&1; }
    retry 60 2 "Timed out waiting for monitor" _mon_ready
}

# Print the OSD id from a device's BlueStore label; fail if it has none.
osd_id_from_label() {
    ceph-bluestore-tool show-label --dev "$1" --no-mon-config | python3 -c '
import json
import sys

data = json.load(sys.stdin)
labels = data.get("devices", data)
for label in labels.values():
    if label.get("description") == "main":
        print(label["whoami"])
        break
else:
    sys.exit(1)
'
}

mkfs_raw_osd() {
    local osd_id="$1" osd_dir="$2" osd_fsid="$3" osd_secret="$4"

    _do_mkfs() {
        printf '%s' "${osd_secret}" | ceph-osd --cluster ceph --conf "${CEPH_CONF}" \
            --osd-objectstore bluestore --mkfs \
            --no-mon-config \
            -i "${osd_id}" \
            --monmap "${osd_dir}/activate.monmap" \
            --osd-data "${osd_dir}" \
            --osd-uuid "${osd_fsid}" \
            --setuser ceph --setgroup ceph \
            --keyfile -
    }
    retry 5 1 "ceph-osd --mkfs failed for osd.${osd_id}" _do_mkfs
}

prepare_raw_osd() {
    local dev="$1"
    local osd_id osd_fsid osd_dir osd_secret

    if osd_id_from_label "${dev}" >/dev/null 2>&1; then
        return 0
    fi

    # Clear any stale label/header from a previous failed prepare.
    dd if=/dev/zero of="${dev}" bs=1M count=1 conv=notrunc status=none 2>/dev/null || true

    osd_fsid="$(uuidgen)"
    osd_secret="$(ceph-authtool --gen-print-key)"
    osd_id="$(printf '{"cephx_secret":"%s"}' "${osd_secret}" | \
        ceph_cmd --cluster ceph --name client.bootstrap-osd \
        --keyring "${BOOTSTRAP_OSD_KEYRING}" -i - osd new "${osd_fsid}")"

    osd_dir="/var/lib/ceph/osd/ceph-${osd_id}"
    rm -rf "${osd_dir}"
    mkdir -p "${osd_dir}"

    ceph-authtool "${osd_dir}/keyring" --create-keyring --name "osd.${osd_id}" \
        --add-key "${osd_secret}" \
        --cap mon 'allow profile osd' \
        --cap mgr 'allow profile osd' \
        --cap osd 'allow *'

    ceph_cmd --cluster ceph --name client.bootstrap-osd \
        --keyring "${BOOTSTRAP_OSD_KEYRING}" \
        mon getmap -o "${osd_dir}/activate.monmap"

    ln -snf "${dev}" "${osd_dir}/block"
    # mkfs runs as ceph and writes into the dir.
    chown -R ceph:ceph "${osd_dir}"
    mkfs_raw_osd "${osd_id}" "${osd_dir}" "${osd_fsid}" "${osd_secret}"
}

start_raw_osd() {
    local dev="$1"
    local osd_id osd_dir

    osd_id="$(osd_id_from_label "${dev}")" || {
        echo "No BlueStore label on ${dev}; skipping osd start" >&2
        return 1
    }
    osd_dir="/var/lib/ceph/osd/ceph-${osd_id}"

    # After mkfs the osd dir is already populated; prime-osd-dir is only needed
    # when re-activating from a labeled device with an empty/missing data dir.
    if [[ ! -f "${osd_dir}/ready" ]]; then
        mkdir -p "${osd_dir}"
        rm -f "${osd_dir}/block" "${osd_dir}/block.wal" "${osd_dir}/block.db"
        ceph-bluestore-tool prime-osd-dir \
            --path "${osd_dir}" \
            --no-mon-config \
            --dev "${dev}"
    fi

    ln -snf "${dev}" "${osd_dir}/block"
    chown -R ceph:ceph "${osd_dir}"
    osd_ids+=("${osd_id}")

    if ! is_running "ceph-osd -i ${osd_id}( |$)"; then
        ceph-osd -i "${osd_id}" --conf "${CEPH_CONF}" --no-mon-config \
            --keyring "${osd_dir}/keyring" \
            --setuser ceph --setgroup ceph -f &
    fi
}

start_osds() {
    local dev
    for dev in "${osd_devices[@]}"; do
        # Device nodes are recreated, root-owned, on every container start.
        chown ceph:ceph "${dev}"
        prepare_raw_osd "${dev}"
    done
    for dev in "${osd_devices[@]}"; do
        start_raw_osd "${dev}"
    done
}

# After a restart the mon still counts the OSDs from before the stop as up,
# so first ask each OSD started here, over its admin socket, whether it has
# booted ("active"); then wait for the mon to agree.
wait_for_osds() {
    local expected="${#osd_devices[@]}"
    [[ "${expected}" -gt 0 ]] || return 0

    _osds_up() {
        local id state up
        for id in "${osd_ids[@]}"; do
            state="$(ceph_cmd daemon "osd.${id}" status 2>/dev/null | json_field state)"
            [[ "${state}" == "active" ]] || return 1
        done
        up="$(ceph_cmd osd stat -f json 2>/dev/null | json_field num_up_osds 0)"
        [[ "${up}" -ge "${expected}" ]]
    }
    retry 60 2 "Timed out waiting for ${expected} OSD(s)" _osds_up
}

wait_for_health() {
    local health

    for _ in $(seq 1 90); do
        health="$(ceph_cmd health 2>/dev/null || true)"
        case "${health}" in
            HEALTH_OK)
                echo "${health}"
                return 0
                ;;
            HEALTH_ERR)
                echo "${health} ($(ceph_cmd health detail 2>/dev/null | tr '\n' ' '))" >&2
                return 1
                ;;
        esac
        sleep 2
    done
    echo "Timed out waiting for cluster health" >&2
    return 1
}

if [[ ! -f "${CEPH_CONF}" ]]; then
    echo "Bootstrapping ceph cluster in ${CLUSTER_DIR}"
    bootstrap_cluster
fi
export CEPH_CONF

# Admin sockets live here; wait_for_osds asks each OSD's for its state.
mkdir -p /var/run/ceph
chown ceph:ceph /var/run/ceph

start_mon
wait_for_mon
start_mgr
wait_for_mgr
setup_dashboard
start_osds
wait_for_osds
wait_for_health

echo "Ceph cluster is running; use: podman exec ${CONTAINER_NAME} ceph -s"
exec tail -f /dev/null
