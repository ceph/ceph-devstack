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
    if pgrep -f "ceph-mon -i ${MON_ID}" >/dev/null 2>&1; then
        return
    fi
    ceph-mon -i "${MON_ID}" --conf "${CEPH_CONF}" --setuser ceph --setgroup ceph -f &
}

start_mgr() {
    if pgrep -f "ceph-mgr -i ${MGR_ID}" >/dev/null 2>&1; then
        return
    fi
    mkdir -p "${CLUSTER_DIR}/var/lib/ceph/mgr/ceph-${MGR_ID}"
    mgr_keyring="${CLUSTER_DIR}/var/lib/ceph/mgr/ceph-${MGR_ID}/keyring"
    for attempt in $(seq 1 30); do
        if ceph --conf "${CEPH_CONF}" auth get-or-create "mgr.${MGR_ID}" \
            mon 'allow profile mgr' osd 'allow *' mds 'allow *' \
            -o "${mgr_keyring}"; then
            break
        fi
        sleep 2
    done
    if [[ ! -f "${mgr_keyring}" ]]; then
        echo "Failed to create mgr.${MGR_ID} keyring" >&2
        return 1
    fi
    chown ceph:ceph "${mgr_keyring}"
    ceph-mgr -i "${MGR_ID}" --conf "${CEPH_CONF}" --no-mon-config \
        --keyring "${mgr_keyring}" \
        --setuser ceph --setgroup ceph -f &
}

wait_for_mgr() {
    local attempt available

    for attempt in $(seq 1 60); do
        available="$(ceph --conf "${CEPH_CONF}" mgr stat -f json 2>/dev/null | python3 -c \
            'import json,sys; print(json.load(sys.stdin).get("available", False))' \
            2>/dev/null || echo False)"
        if [[ "${available}" == "True" ]]; then
            return 0
        fi
        sleep 2
    done
    echo "Timed out waiting for mgr" >&2
    return 1
}

setup_dashboard() {
    local mon_ip scheme url port_option password_file

    if [[ "${DASHBOARD_ENABLED}" != "true" ]]; then
        return 0
    fi

    password_file="${CLUSTER_DIR}/dashboard-admin-secret.txt"
    mon_ip="$(detect_mon_ip)"

    ceph --conf "${CEPH_CONF}" mgr module enable dashboard

    if [[ "${DASHBOARD_SSL}" == "true" ]]; then
        port_option="ssl_server_port"
        scheme="https"
        ceph --conf "${CEPH_CONF}" config set mgr mgr/dashboard/ssl true --force
        ceph --conf "${CEPH_CONF}" config set mgr \
            "mgr/dashboard/${MGR_ID}/${port_option}" "${DASHBOARD_PORT}" --force
        ceph --conf "${CEPH_CONF}" dashboard create-self-signed-cert 2>/dev/null || true
    else
        port_option="server_port"
        scheme="http"
        ceph --conf "${CEPH_CONF}" config set mgr mgr/dashboard/ssl false --force
        ceph --conf "${CEPH_CONF}" config set mgr \
            "mgr/dashboard/${MGR_ID}/${port_option}" "${DASHBOARD_PORT}" --force
    fi
    ceph --conf "${CEPH_CONF}" config set mgr \
        "mgr/dashboard/${MGR_ID}/server_addr" "0.0.0.0" --force

    for attempt in $(seq 1 60); do
        if ceph --conf "${CEPH_CONF}" -h 2>/dev/null | grep -q '^dashboard '; then
            break
        fi
        sleep 2
    done

    printf '%s' "${DASHBOARD_PASSWORD}" > "${password_file}"
    chown ceph:ceph "${password_file}"
    chmod 600 "${password_file}"

    if ceph --conf "${CEPH_CONF}" dashboard ac-user-show "${DASHBOARD_USER}" >/dev/null 2>&1; then
        ceph --conf "${CEPH_CONF}" dashboard ac-user-set-password "${DASHBOARD_USER}" \
            -i "${password_file}"
    else
        ceph --conf "${CEPH_CONF}" dashboard ac-user-create "${DASHBOARD_USER}" \
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
    local attempt
    for attempt in $(seq 1 60); do
        if ceph --conf "${CEPH_CONF}" -s >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    echo "Timed out waiting for monitor" >&2
    return 1
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
    local attempt

    for attempt in $(seq 1 5); do
        if printf '%s' "${osd_secret}" | ceph-osd --cluster ceph --conf "${CEPH_CONF}" \
            --osd-objectstore bluestore --mkfs \
            --no-mon-config \
            -i "${osd_id}" \
            --monmap "${osd_dir}/activate.monmap" \
            --osd-data "${osd_dir}" \
            --osd-uuid "${osd_fsid}" \
            --setuser ceph --setgroup ceph \
            --keyfile -; then
            return 0
        fi
        echo "ceph-osd --mkfs failed for osd.${osd_id} (attempt ${attempt}/5)" >&2
        sleep 1
    done
    return 1
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
        ceph --conf "${CEPH_CONF}" --cluster ceph --name client.bootstrap-osd \
        --keyring "${BOOTSTRAP_OSD_KEYRING}" -i - osd new "${osd_fsid}")"

    osd_dir="/var/lib/ceph/osd/ceph-${osd_id}"
    rm -rf "${osd_dir}"
    mkdir -p "${osd_dir}"

    ceph-authtool "${osd_dir}/keyring" --create-keyring --name "osd.${osd_id}" \
        --add-key "${osd_secret}" \
        --cap mon 'allow profile osd' \
        --cap mgr 'allow profile osd' \
        --cap osd 'allow *'

    ceph --conf "${CEPH_CONF}" --cluster ceph --name client.bootstrap-osd \
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

    if ! pgrep -f "ceph-osd -i ${osd_id}" >/dev/null 2>&1; then
        ceph-osd -i "${osd_id}" --conf "${CEPH_CONF}" --no-mon-config \
            --keyring "${osd_dir}/keyring" \
            --setuser ceph --setgroup ceph -f &
    fi
}

wait_for_osds() {
    local attempt expected up
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

    for attempt in $(seq 1 60); do
        up="$(ceph --conf "${CEPH_CONF}" osd stat -f json 2>/dev/null | python3 -c \
            'import json,sys; print(json.load(sys.stdin).get("num_osds_up", 0))' 2>/dev/null || echo 0)"
        if [[ "${up}" -ge "${expected}" ]]; then
            return 0
        fi
        sleep 2
    done
    echo "Timed out waiting for ${expected} OSD(s)" >&2
    return 1
}

wait_for_health() {
    local attempt health

    for attempt in $(seq 1 90); do
        health="$(ceph --conf "${CEPH_CONF}" health 2>/dev/null || true)"
        case "${health}" in
            HEALTH_OK)
                echo "${health}"
                return 0
                ;;
            HEALTH_ERR)
                echo "${health} ($(ceph --conf "${CEPH_CONF}" health detail 2>/dev/null | tr '\n' ' '))" >&2
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
