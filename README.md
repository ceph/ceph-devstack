# ceph-devstack
A tool for testing [Ceph](https://github.com/ceph/ceph) locally using [nested rootless podman containers](https://www.redhat.com/sysadmin/podman-inside-container)

## Overview
ceph-devstack is a tool that can deploy and manage containerized versions of [teuthology](https://github.com/ceph/teuthology) and its associated services, to test Ceph (or just teuthology) on your local machine. It lets you avoid:

- Accessing Ceph's [Sepia lab](https://wiki.sepia.ceph.com/)
- Needing dedicated storage devices to test Ceph OSDs

Basically, the goal is that you can test your Ceph branch locally using containers
as storage test nodes.

It is currently under active development and has not yet had a formal release.

## Supported Operating Systems

☑︎ CentOS 9.Stream should work out of the box

☑︎ CentOS 8.Stream mostly works - but has not yet passed a Ceph test

☐ A recent Fedora should work but has not been tested

☒ Ubuntu does not currently ship a new enough podman

☒ MacOS will require special effort to support since podman operations are done inside a VM

## Requirements

* A supported operating system
* podman 4.0+ using the `crun` runtime.
  * On CentOS 8, modify `/etc/containers/containers.conf` to set the runtime
* Linux kernel 5.12+, or 4.15+ _and_ `fuse-overlayfs`
* cgroup v2
  * On CentOS 8, see [./docs/cgroup_v2.md](./docs/cgroup_v2.md)
* With podman <5.0, podman's DNS plugin, from the `podman-plugins` package
* A user account that has `sudo` access and also is a member of the `disk` group
* The following sysctl settings:
  * `fs.aio-max-nr=1048576`
  * `kernel.pid_max=4194304`
* If using SELinux in enforcing mode:
  * `setsebool -P container_manage_cgroup=true`
  * `setsebool -P container_use_devices=true`

`ceph-devstack doctor` will check the above and report any issues along with suggested remedies; its `--fix` flag will apply them for you.

## Setup

```bash
sudo usermod -a -G disk $(whoami)  # and re-login afterward
git clone https://github.com/ceph/teuthology/
cd teuthology && ./bootstrap
python3 -m venv venv
source ./venv/bin/activate
python3 -m pip install git+https://github.com/zmc/ceph-devstack.git
```

## Configuration
`ceph-devstack` 's default configuration is [here](./ceph_devstack/config.toml). It can be extended by placing a file at `~/.config/ceph-devstack/config.toml` or by using the `--config-file` flag.

`ceph-devstack config dump` will output the current configuration.

### Shared block pool (optional)

To back loop devices with slices of a real NVMe device instead of sparse files,
configure `[block_pool]` in your config. The pool is shared across `ceph_node`
and all `testnode_*` containers. Each loop device uses that container's
`loop_device_size` for its backing region on the pool parent.

```toml
[block_pool]
parent = "/dev/nvme0n1p1"   # or a dedicated whole disk
allow_enroll = true         # first run only; unset afterward

[containers.ceph_node]
loop_device_size = "5G"

[containers.testnode]
loop_device_size = "5G"
```

Requirements:

* Your user must be in the `disk` group and able to read/write the parent
  device directly (enrollment writes a tail marker to the raw device).
* The parent must be empty, or already enrolled by ceph-devstack (tail marker
  at the end of the device).

`ceph-devstack block-pool status` shows current allocations.

Each container picks host loop devices at create time: it needs
`loop_device_count` devices of `loop_device_size`, reuses any existing backing
files for that container name, then takes the lowest free loop numbers after
scanning what is already attached or claimed under `data_dir/disk_images`.

If `block_pool.json` is lost but the on-disk marker remains, delete the state
file and start again; the pool reclaims the marker without reformatting the
device.

### Ceph stack

To run a single-container local Ceph cluster instead of teuthology:

```bash
ceph-devstack --stack ceph pull
ceph-devstack --stack ceph create
ceph-devstack --stack ceph start
podman logs -f ceph_node
podman exec ceph_node ceph -c /var/lib/ceph-devstack/cluster/ceph.conf -s
```

The dashboard listens on port 8080 by default (`admin` / `admin`). Set
`dashboard_show_password = true` under `[containers.ceph_node]` to print the
password in the container log.

As an example, the following configuration will use a local image for paddles with the tag `TEST`; it will also create ten testnode containers; and will build its teuthology container from the git repo at `~/src/teuthology`:
```
containers:
  paddles:
    image: localhost/paddles:TEST
  testnode:
    count: 10
  teuthology:
    repo: ~/src/teuthology
```
## Usage
By default, pre-built container images are pulled from [quay.io/ceph-infra](https://quay.io/organization/ceph-infra). The images can be overridden via the config file. It's also possible to _build_ images from on-disk git repositories.

First, you'll want to pull all the images:

```bash
ceph-devstack pull
```

Optional: If building any images from repos:
```bash
ceph-devstack build
```

Next, you can start the containers with:

```bash
ceph-devstack start
```

Once everything is started, a message similar to this will be logged:

`View test results at http://smithi065.front.sepia.ceph.com:8081/`

This link points to the running Pulpito instance. Test archives are also stored in the `--data-dir` (default: `~/.local/share/ceph-devstack`).

To watch teuthology's output, you can:

```bash
podman logs -f teuthology
```

If you want testnode containers to be replaced as they are stopped and destroyed, you can:

```bash
ceph-devstack watch
```

When finished, this command removes all the resources that were created:

```bash
ceph-devstack remove
```

### Specifying a Test Suite
By default, we run the `teuthology:no-ceph` suite to self-test teuthology. If we wanted to test Ceph itself, we could use the `orch:cephadm:smoke-small` suite:

```bash
export TEUTHOLOGY_SUITE=orch:cephadm:smoke-small
```

It's possible to skip the automatic suite-scheduling behavior:

```bash
export TEUTHOLOGY_SUITE=none
```

### Using testnodes from an existing lab
If you need to use "real" testnodes and have access to a lab, there are a few additonal steps to take. We will use the Sepia lab as an example below:

To give the teuthology container access to your SSH private key (via `podman secret`):

```bash
export SSH_PRIVKEY_PATH=$HOME/.ssh/id_rsa
```

To lock machines from the lab:

```bash
ssh teuthology.front.sepia.ceph.com
~/teuthology/virtualenv/bin/teuthology-lock \
  --lock-many 1 \
  --machine-type smithi \
  --desc "teuthology dev testing"
```

Once you have your machines locked, you need to provide a list of their hostnames and their machine type:

```bash
export TEUTHOLOGY_TESTNODES="smithiXXX.front.sepia.ceph.com,smithiYYY.front.sepia.ceph.com"
export TEUTHOLOGY_MACHINE_TYPE="smithi"
```

### Setup for development

1. First fork the repo if you have not done so.
2. Clone your forked repo
```bash
git clone https://github.com/<user-name>/ceph-devstack
```

3. Setup the remote repo as upstream (this will prevent creating additional branches)
```bash
git remote add upstream https://github.com/zmc/ceph-devstack
```

4. Create virtual env in the root directory of ceph-devstack & install python dependencies
```bash
python3 -m venv venv
./venv/bin/pip3 install -e .
```

5. Activate venv
```bash
source venv/bin/activate
```

6. Run doctor command to check & fix the dependencies that you need for ceph-devstack
```bash
ceph-devstack -v doctor --fix
```

7. Build, Create and Start the all containers in ceph-devstack
```bash
ceph-devstack -v build
ceph-devstack -v create
ceph-devstack -v start
```

8. Test the containers by waiting for teuthology to finish and print the logs
```bash
ceph-devstack wait teuthology
podman logs -f teuthology
```
