#!/usr/bin/env bash
set -euo pipefail

# Run only on disposable GitHub-hosted Ubuntu VMs, not on existing Incus hosts.
# Ubuntu 24.04 ships Incus 6.0, which rejects the microarchitecture variant
# labels used by some upstream images. Install Incus 7.5 from the Zabbly repo.
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc
sudo chmod 0644 /etc/apt/keyrings/zabbly.asc
echo "deb [signed-by=/etc/apt/keyrings/zabbly.asc] https://pkgs.zabbly.com/incus/stable noble main" \
  | sudo tee /etc/apt/sources.list.d/zabbly-incus-stable.list
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

# Query the supported JSON listing for remote discovery. Keep this outside the
# conditional so command or parsing failures stop the script rather than being
# mistaken for absence.
remote_names=$(sudo incus remote list --format=json | python3 -c \
  'import json, sys; print("\n".join(json.load(sys.stdin)))')
if ! grep -Fxq images <<<"$remote_names"; then
  sudo incus remote add images https://images.linuxcontainers.org \
    --protocol=simplestreams --public
fi
incus_version=$(sudo incus version)
if ! grep -qE '^Client version: 7\.5\.' <<<"$incus_version" || \
   ! grep -qE '^Server version: 7\.5\.' <<<"$incus_version"; then
  printf '[错误] 需要 Incus 7.5，当前版本：\n%s\n' "$incus_version" >&2
  exit 1
fi
sudo incus remote list --format=json
sudo incus storage list
sudo incus network list
df -h
