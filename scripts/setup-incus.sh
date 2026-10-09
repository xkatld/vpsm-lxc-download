#!/usr/bin/env bash
set -euo pipefail

# Run only on disposable GitHub-hosted Ubuntu VMs, not on existing Incus hosts.
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  incus openssh-client sshpass openssl ca-certificates
sudo systemctl start incus
sudo timeout 120 incus admin waitready

# A minimal initialization alone does not guarantee a usable container NIC.
# Explicitly provide NAT/DHCP and a directory-backed storage pool (no loop disk).
sudo incus admin init --preseed <<'YAML'
config:
  images.auto_update_interval: "0"
networks:
  - name: incusbr0
    type: bridge
    config:
      ipv4.address: auto
      ipv4.nat: "true"
      ipv6.address: none
storage_pools:
  - name: default
    driver: dir
profiles:
  - name: default
    devices:
      root:
        path: /
        pool: default
        type: disk
      eth0:
        name: eth0
        network: incusbr0
        type: nic
YAML

# Docker is preinstalled on hosted runners and can set FORWARD to DROP.
# Permit only this job's bridge traffic, without disabling the host firewall.
sudo iptables -I FORWARD 1 -i incusbr0 -j ACCEPT
sudo iptables -I FORWARD 1 -o incusbr0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

if ! sudo incus remote get-url images >/dev/null 2>&1; then
  sudo incus remote add images https://images.linuxcontainers.org \
    --protocol=simplestreams --public
fi
sudo incus version
sudo incus remote get-url images
sudo incus storage list
sudo incus network list
df -h
