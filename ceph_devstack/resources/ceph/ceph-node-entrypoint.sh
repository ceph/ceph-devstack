#!/bin/bash
set -euo pipefail

CLUSTER_DIR="${CLUSTER_DIR:?CLUSTER_DIR is required}"
CEPH_CONF="${CLUSTER_DIR}/ceph.conf"
ADMIN_KEYRING="${CLUSTER_DIR}/ceph.client.admin.keyring"
MON_ID="${MON_ID:-a}"
MGR_ID="${MGR_ID:-x}"
OSD_DEVICES="${OSD_DEVICES:-}"
DASHBOARD_ENABLED="${DASHBOARD_ENABLED:-true}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
DASHBOARD_SSL="${DASHBOARD_SSL:-false}"
DASHBOARD_USER="${DASHBOARD_USER:-admin}"
DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-admin}"
BOOTSTRAP_DIR="${CLUSTER_DIR}/bootstrap"
BOOTSTRAP_OSD_KEYRING="/var/lib/ceph/bootstrap-osd/ceph.keyring"

export CEPH_VOLUME_ALLOW_LOOP_DEVICES=true

ceph_cmd() { ceph --conf "${CEPH_CONF}" "$@"; }

retry() {
    local max_attempts="$1" sleep_secs="$2" fail_msg="$3"
    shift 3
    local attempt
    for attempt in $(seq 1 "${max_attempts}"); do
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

is_running() { pgrep -f "$1" >/dev/null 2>&1; }

mkdir -p "${CLUSTER_DIR}/var/lib/ceph"

export_ceph_conf() {
    if [[ -f "${CEPH_CONF}" ]]; then
        export CEPH_CONF
    else
        unset CEPH_CONF || true
    fi
}

chown_cluster() {
    chown -R ceph:ceph /var/lib/ceph
    for path in "${CEPH_CONF}" "${ADMIN_KEYRING}" "${CLUSTER_DIR}/fsid"; do
        if [[ -e "${path}" ]]; then
            chown ceph:ceph "${path}"
        fi
    done
}

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

bootstrap_cluster() {
    local fsid mon_ip public_network mon_keyring monmap mon_host
    fsid="$(cat "${CLUSTER_DIR}/fsid" 2>/dev/null || uuidgen | tee "${CLUSTER_DIR}/fsid")"
    mon_ip="$(detect_mon_ip)"
    public_network="$(detect_public_network "${mon_ip}")"
    mon_host="v2:${mon_ip}:3300,v1:${mon_ip}:6789"
    mon_keyring="${BOOTSTRAP_DIR}/ceph.mon.keyring"
    monmap="${BOOTSTRAP_DIR}/ceph.monmap"

    mkdir -p "${BOOTSTRAP_DIR}"
    mkdir -p "${CLUSTER_DIR}/var/lib/ceph/mon/ceph-${MON_ID}"
    mkdir -p "${CLUSTER_DIR}/var/lib/ceph/mgr/ceph-${MGR_ID}"
    mkdir -p "${CLUSTER_DIR}/var/lib/ceph/bootstrap-osd"

    ceph-authtool --create-keyring "${mon_keyring}" --gen-key -n mon. --cap mon 'allow *'
    ceph-authtool "${mon_keyring}" --gen-key -n client.admin \
        --cap mon 'allow *' \
        --cap osd 'allow *' \
        --cap mds 'allow *' \
        --cap mgr 'allow *'
    ceph-authtool --create-keyring \
        "${CLUSTER_DIR}/var/lib/ceph/bootstrap-osd/ceph.keyring" \
        --gen-key -n client.bootstrap-osd \
        --cap mon 'profile bootstrap-osd'
    ceph-authtool "${mon_keyring}" --import-keyring \
        "${CLUSTER_DIR}/var/lib/ceph/bootstrap-osd/ceph.keyring"

    monmaptool --create --clobber --fsid "${fsid}" \
        --addv "${MON_ID}" "[${mon_host}]" \
        "${monmap}"

    cat > "${CEPH_CONF}" <<EOF
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
osd pool default size = 2          # three OSDs in the default layout; one host may be out
osd pool default min size = 1
osd crush chooseleaf type = 0
mgr/cephadm/use_agent = false

[client]
keyring = ${ADMIN_KEYRING}

[mon.${MON_ID}]
host = $(hostname -s 2>/dev/null || hostname)
public bind addr =

[osd]
osd numa auto affinity = false
EOF
    export_ceph_conf

    ceph-mon --mkfs -i "${MON_ID}" --monmap "${monmap}" --keyring "${mon_keyring}" \
        --conf "${CEPH_CONF}"

    ceph-authtool --create-keyring "${ADMIN_KEYRING}"
    ceph-authtool "${ADMIN_KEYRING}" \
        --import-keyring "${mon_keyring}"

    chown_cluster
    rm -rf "${BOOTSTRAP_DIR}"
}

start_mon() {
    is_running "ceph-mon -i ${MON_ID}" && return
    ceph-mon -i "${MON_ID}" --conf "${CEPH_CONF}" --setuser ceph --setgroup ceph -f &
}

start_mgr() {
    is_running "ceph-mgr -i ${MGR_ID}" && return
    mkdir -p "${CLUSTER_DIR}/var/lib/ceph/mgr/ceph-${MGR_ID}"
    mgr_keyring="${CLUSTER_DIR}/var/lib/ceph/mgr/ceph-${MGR_ID}/keyring"
    retry 30 2 "Failed to create mgr.${MGR_ID} keyring" \
        ceph_cmd auth get-or-create "mgr.${MGR_ID}" \
        mon 'allow profile mgr' osd 'allow *' mds 'allow *' \
        -o "${mgr_keyring}"
    chown ceph:ceph "${mgr_keyring}"
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
    local mon_ip scheme url port_option password_file

    if [[ "${DASHBOARD_ENABLED}" != "true" ]]; then
        return 0
    fi

    password_file="${CLUSTER_DIR}/dashboard-admin-secret.txt"
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

    _dashboard_cli_ready() { ceph_cmd -h 2>/dev/null | grep -q '^dashboard '; }
    retry 60 2 "Timed out waiting for dashboard CLI" _dashboard_cli_ready

    printf '%s' "${DASHBOARD_PASSWORD}" > "${password_file}"
    chown ceph:ceph "${password_file}"
    chmod 600 "${password_file}"

    if ceph_cmd dashboard ac-user-show "${DASHBOARD_USER}" >/dev/null 2>&1; then
        ceph_cmd dashboard ac-user-set-password "${DASHBOARD_USER}" \
            -i "${password_file}"
    else
        ceph_cmd dashboard ac-user-create "${DASHBOARD_USER}" \
            -i "${password_file}" administrator --force-password
    fi

    url="${scheme}://${mon_ip}:${DASHBOARD_PORT}/"
    echo "Ceph dashboard: ${url}"
    echo "  user: ${DASHBOARD_USER}"
    if [[ "${DASHBOARD_SHOW_PASSWORD:-false}" == "true" ]]; then
        echo "  password: ${DASHBOARD_PASSWORD}"
    else
        echo "  password: (see dashboard_password in config; set DASHBOARD_SHOW_PASSWORD=true to log it)"
    fi
}

wait_for_mon() {
    _mon_ready() { ceph_cmd -s >/dev/null 2>&1; }
    retry 60 2 "Timed out waiting for monitor" _mon_ready
}

bluestore_show_label() {
    local dev="$1"
    ceph-bluestore-tool show-label --dev "${dev}" --no-mon-config
}

get_osd_id_from_device() {
    local dev="$1"
    bluestore_show_label "${dev}" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
labels = data.get("devices", data)
for label in labels.values():
    if label.get("description") == "main":
        print(label["whoami"])
        break
'
}

device_has_osd_label() {
    local dev="$1"
    get_osd_id_from_device "${dev}" >/dev/null 2>&1
}

chown_block_device() {
    local dev="$1"
    chown ceph:ceph "${dev}"
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

    if device_has_osd_label "${dev}"; then
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

    chown_block_device "${dev}"
    ln -snf "${dev}" "${osd_dir}/block"
    chown -R ceph:ceph "${osd_dir}"

    mkfs_raw_osd "${osd_id}" "${osd_dir}" "${osd_fsid}" "${osd_secret}"
    chown -R ceph:ceph "${osd_dir}"
}

start_raw_osd() {
    local dev="$1"
    local osd_id osd_dir

    if ! device_has_osd_label "${dev}"; then
        echo "No BlueStore label on ${dev}; skipping osd start" >&2
        return 1
    fi

    osd_id="$(get_osd_id_from_device "${dev}")"
    osd_dir="/var/lib/ceph/osd/ceph-${osd_id}"
    mkdir -p "${osd_dir}"

    chown_block_device "${dev}"

    # After mkfs the osd dir is already populated; prime-osd-dir is only needed
    # when re-activating from a labeled device with an empty/missing data dir.
    if [[ ! -f "${osd_dir}/ready" ]]; then
        rm -f "${osd_dir}/block" "${osd_dir}/block.wal" "${osd_dir}/block.db"
        chown -R ceph:ceph "${osd_dir}"
        ceph-bluestore-tool prime-osd-dir \
            --path "${osd_dir}" \
            --no-mon-config \
            --dev "${dev}"
    fi

    ln -snf "${dev}" "${osd_dir}/block"
    chown -R ceph:ceph "${osd_dir}"

    if ! is_running "ceph-osd -i ${osd_id}"; then
        ceph-osd -i "${osd_id}" --conf "${CEPH_CONF}" --no-mon-config \
            --keyring "${osd_dir}/keyring" \
            --setuser ceph --setgroup ceph -f &
    fi
}

wait_for_osds() {
    local expected
    local -a devices=()

    if [[ -z "${OSD_DEVICES}" ]]; then
        return 0
    fi
    IFS=',' read -ra devices <<< "${OSD_DEVICES}"
    expected=0
    for dev in "${devices[@]}"; do
        [[ -b "${dev}" ]] && expected=$((expected + 1))
    done
    [[ "${expected}" -gt 0 ]] || return 0

    _osds_up() {
        local up
        up="$(ceph_cmd osd stat -f json 2>/dev/null | json_field num_osds_up 0)"
        [[ "${up}" -ge "${expected}" ]]
    }
    retry 60 2 "Timed out waiting for ${expected} OSD(s)" _osds_up
}

wait_for_health() {
    local attempt health

    for attempt in $(seq 1 90); do
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

prepare_osds() {
    local dev
    if [[ -z "${OSD_DEVICES}" ]]; then
        return
    fi
    IFS=',' read -ra devices <<< "${OSD_DEVICES}"
    for dev in "${devices[@]}"; do
        [[ -b "${dev}" ]] || continue
        prepare_raw_osd "${dev}"
    done
    for dev in "${devices[@]}"; do
        [[ -b "${dev}" ]] || continue
        start_raw_osd "${dev}"
    done
}

if [[ ! -f "${CEPH_CONF}" ]]; then
    echo "Bootstrapping ceph cluster in ${CLUSTER_DIR}"
    bootstrap_cluster
else
    export_ceph_conf
fi

start_mon
wait_for_mon
start_mgr
wait_for_mgr
setup_dashboard
prepare_osds
wait_for_osds
wait_for_health

echo "Ceph cluster is running; use: podman exec ${CONTAINER_NAME:-ceph_node} ceph -s"
exec tail -f /dev/null
