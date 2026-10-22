# Flowly cloud machine: one per user, on Fly Machines (docs/cloud/DECISIONS.md D20).
# Build context: the directory cloud/image/build-context.sh prepares (this
# repository's own tracked sources, never GitHub main or the public installer).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        systemd systemd-sysv dbus libpam-systemd curl ca-certificates git sudo util-linux procps \
        iproute2 nftables tzdata python3 socat \
    && rm -rf /var/lib/apt/lists/*

# uv pinned, as in the screen integration runners.
COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /usr/local/bin/uv
ENV FLOWLY_SRC=/opt/flowly/repo \
    FLOWLY_VENV=/opt/flowly/venv \
    UV_PROJECT_ENVIRONMENT=/opt/flowly/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

# Locked dependencies first (cached layer), then Core itself as an editable
# checkout in /opt/flowly/repo, the same layout scripts/install.sh produces.
WORKDIR /opt/flowly/repo
COPY src/pyproject.toml src/uv.lock src/README.md src/PYPI_README.md src/LICENSE src/NOTICE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/flowly ./flowly
COPY src/bridge ./bridge
COPY src/workspace ./workspace
COPY build-info.json /opt/flowly/build-info.json
RUN uv sync --frozen --no-dev \
    && ln -sf /opt/flowly/venv/bin/flowly /usr/local/bin/flowly \
    && flowly --version
WORKDIR /

# The screen's system packages, Google Chrome (D17) and Python packages, baked in.
# Its accounts are removed again: they are recreated at every boot from the
# receipts on the volume (flowly screen restore-accounts), the same path a
# redeployed or cold-started machine takes, so the image never decides an
# account's UID.
RUN HOME=/root flowly screen install --yes --no-restart \
    && HOME=/root flowly screen capabilities | grep -q '"supported": *true' \
    && userdel flowly-screen \
    && ! getent passwd flowly-screen && ! getent group flowly-screen \
    && test -f /var/lib/flowly-screen-runtime/account.json

# Machine time zone is the user's (D17), applied at boot from FLOWLY_TZ.
RUN ln -sf /usr/share/zoneinfo/Etc/UTC /etc/localtime && echo Etc/UTC > /etc/timezone

COPY init.sh /init.sh
COPY flowly-cloud-setup /usr/local/sbin/flowly-cloud-setup
COPY flowly_cloud_config.py /opt/flowly/cloud/flowly_cloud_config.py
COPY systemd/flowly-cloud-setup.service systemd/flowly-edge.service /etc/systemd/system/
COPY systemd/user-0-flowly.conf /etc/systemd/system/user@0.service.d/flowly.conf
COPY systemd/ai.flowly.gateway.service /etc/systemd/user/ai.flowly.gateway.service
RUN chmod 755 /init.sh /usr/local/sbin/flowly-cloud-setup \
    && systemctl enable flowly-cloud-setup.service flowly-edge.service \
    && systemctl --global enable ai.flowly.gateway.service \
    && mkdir -p /var/lib/systemd/linger && touch /var/lib/systemd/linger/root \
    && systemctl mask systemd-udevd.service systemd-udevd-kernel.socket systemd-udevd-control.socket \
        getty.target console-getty.service systemd-firstboot.service systemd-networkd.service \
        systemd-resolved.service systemd-binfmt.service

STOPSIGNAL SIGRTMIN+3
CMD ["/init.sh"]
