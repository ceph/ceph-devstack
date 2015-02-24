#!/usr/bin/env bash

# Keep track of the devstack directory
TOP_DIR=$(cd $(dirname "$0") && pwd)

# Import common functions
source $TOP_DIR/functions

# Use openrc + stackrc + localrc for settings
source $TOP_DIR/stackrc

# Destination path for installation ``DEST``
DEST=${DEST:-/opt/stack}

if is_service_enabled nova; then

    # Import ssh keys
    # ---------------

    # Get OpenStack admin auth
    source $TOP_DIR/openrc admin admin

    rm -f admin.pem
    nova keypair-add admin > admin.pem
    chmod 400 admin.pem

    # Create A Flavor
    # ---------------

    # Name of new flavor
    MI_NANO=m1.nano
    MI_MICRO=m1.micro

    # Create nano flavor if not present
    if [[ -z $(nova flavor-list | grep $MI_NANO) ]]; then
        nova flavor-create $MI_NANO 6 64 0 1
    fi

    # Create micro flavor if not present
    if [[ -z $(nova flavor-list | grep $MI_MICRO) ]]; then
        nova flavor-create $MI_MICRO 7 128 0 1
    fi

    # Create security group rules
    # ----------

    # Add tcp/22 and icmp to default security group
    nova secgroup-add-rule default tcp 22 22 0.0.0.0/0
    nova secgroup-add-rule default icmp -1 -1 0.0.0.0/0
fi

if is_service_enabled ceph; then

  # Download images, convert to RAW and upload them
  # ----------

  wget http://download.cirros-cloud.net/0.3.3/cirros-0.3.3-x86_64-disk.img
  sudo qemu-img convert -f qcow2 -O raw cirros-0.3.3-x86_64-disk.img cirros-0.3.3-x86_64-disk.raw
  glance image-create --name CirrOS-0.3.3 --disk-format raw --container-format bare --file cirros-0.3.3-x86_64-disk.raw --progress

  wget https://cloud-images.ubuntu.com/trusty/current/trusty-server-cloudimg-amd64-disk1.img
  sudo qemu-img convert -f qcow2 -O raw trusty-server-cloudimg-amd64-disk1.img trusty-server-cloudimg-amd64-disk1.raw
  glance image-create --name Ubuntu-14.04 --disk-format raw --container-format bare --file trusty-server-cloudimg-amd64-disk1.raw --progress
fi
