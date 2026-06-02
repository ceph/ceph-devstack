pipeline {
  agent { node {
    label "centos9 && x86_64"
  }}
  environment {
    CDS_CONF = "${env.WORKSPACE}/ceph-devstack.toml"
    PATH = "${env.PATH}:${env.HOME}/.local/bin"
  }
  stages {
    stage("Setup system") {
      steps {
        script {
          env.OLD_AIO_MAX_NR = """${sh(returnStdout: true, script: "sysctl -b fs.aio-max-nr")}"""
        }
        sh """
          sudo dnf install -y podman podman-plugins policycoreutils-devel selinux-policy-devel pipx
          pipx install uv
          sudo dnf update -y container\\* podman\\* selinux\\*
          sudo sysctl fs.aio-max-nr=1048576
          sudo usermod -a -G disk ${env.USER}
          mkdir -p ~/.local/share/containers
          # The below command is not idempotent, hence the '|| true'.
          # See https://patchwork.kernel.org/project/selinux/patch/20240214122706.522873-1-vmojzis@redhat.com/
          sudo semanage fcontext -a -e /var/lib/containers ~/.local/share/containers || true
          sudo restorecon -R ~/.local/share/containers
          sudo setsebool -P container_manage_cgroup=true
          sudo setsebool -P container_use_devices=true
          cd ${env.WORKSPACE}/ceph_devstack
          make -f /usr/share/selinux/devel/Makefile ceph_devstack.pp
          sudo semodule -i ceph_devstack.pp
        """
      }
    }
    stage("Clone teuthology") {
      steps {
        sh """
        git clone -b ${env.TEUTHOLOGY_BRANCH} https://github.com/ceph/teuthology ${env.WORKSPACE}/teuthology
        """
      }
    }
    stage("Setup ceph-devstack") {
      steps {
        sh """
          python3 -V
          uv venv
          uv run python -V
          uv pip install -e .
          uv sync --frozen --all-extras
          touch ${env.CDS_CONF}
          uv run ceph-devstack --config-file ${env.CDS_CONF} config set data_dir '${env.WORKSPACE}/data'
          if [ -n ${env.TEUTHOLOGY_BRANCH} ]; then
            uv run ceph-devstack --config-file ${env.CDS_CONF} config set containers.teuthology.repo '${env.WORKSPACE}/teuthology'
          fi
          uv run ceph-devstack --config-file ${env.CDS_CONF} config dump
          uv run ceph-devstack --config-file ${env.CDS_CONF} doctor --fix
        """
      }
    }
    stage("Build container images") {
      steps {
        sh """
          uv run ceph-devstack -v --config-file ${env.CDS_CONF} build
        """
      }
    }
    stage("Pull container images") {
      steps {
        sh """
          uv run ceph-devstack -v --config-file ${env.CDS_CONF} pull
        """
      }
    }
    stage("Create containers") {
      steps {
        sh """
          uv run ceph-devstack --config-file ${env.CDS_CONF} -v create
        """
      }
    }
    stage("Start containers") {
      steps {
        sh """
          uv run ceph-devstack --config-file ${env.CDS_CONF} -v start
        """
      }
    }
    stage("Wait for teuthology container") {
      steps {
        sh """
          podman wait teuthology
          exit \$(podman inspect -f "{{.State.ExitCode}}" teuthology)
        """
      }
    }
  }
  post {
    always {
      sh """
        podman logs teuthology
      """
      sh """
        mkdir -p data/containers
        podman logs teuthology 2>&1 > data/containers/teuthology.log
        uv run ceph-devstack --config-file ${env.CDS_CONF} -v remove
        podman volume prune -f
        podman ps -a
        sudo sysctl fs.aio-max-nr=${env.OLD_AIO_MAX_NR}
      """
      archiveArtifacts artifacts: 'ceph-devstack.yml,data/archive/**', fingerprint: true
    }
  }
}
