#!/bin/sh
# Install the built ISO into a QEMU VM unattended (software emulation), boot the result, and check the configuration
# over SSH. Only difference from a real install: the clock's name and number and the disk confirmations are answered on the
# kernel command line. Mounts: /out (the ISO), /work, /keys/id (the workstation's private key).
set -eu
cd /work
ISO=/out/$(ls /out | grep '^timeclock-debian-.*\.iso$' | head -n1)
rm -rf vm && mkdir vm
xorriso -osirrox on -indev "$ISO" -extract /install.amd/vmlinuz vm/vmlinuz \
  -extract /install.amd/initrd.gz vm/initrd.gz >/dev/null 2>&1
qemu-img create -q -f qcow2 vm/disk.qcow2 16G
install -m 600 /keys/id vm/id

echo "== install ($(date -u +%H:%M))"
timeout 5h qemu-system-x86_64 -m 2048 -smp 2 -accel tcg -nographic -no-reboot \
  -drive file=vm/disk.qcow2,if=virtio -cdrom "$ISO" \
  -kernel vm/vmlinuz -initrd vm/initrd.gz \
  -append "auto=true priority=critical partman/confirm=true partman/confirm_nooverwrite=true timeclock/instance-id=test timeclock/clock-number=98 console=ttyS0,115200n8" \
  -netdev user,id=n0 -device virtio-net-pci,netdev=n0 > vm/install.log 2>&1 || true
echo "== install ended ($(date -u +%H:%M)); last lines:"
tail -c 2000 vm/install.log | tr -cd '\11\12\15\40-\176' | tail -n 15

echo "== boot"
qemu-system-x86_64 -m 2048 -smp 2 -accel tcg -nographic \
  -drive file=vm/disk.qcow2,if=virtio \
  -netdev user,id=n0,hostfwd=tcp::2222-:22 -device virtio-net-pci,netdev=n0 > vm/boot.log 2>&1 &
QEMU=$!
SSH="ssh -i vm/id -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o BatchMode=yes tcadmin@localhost"
i=0
until $SSH true 2>/dev/null; do
  i=$((i + 1)); [ $i -lt 120 ] || { echo "no SSH after 20 min"; tail -n 30 vm/boot.log; kill $QEMU; exit 1; }
  sleep 10
done

echo "== checks"
$SSH '
  set -x
  [ "$(hostname)" = timeclock-system-test ] && echo "hostname timeclock-system-test: OK" || hostname
  sudo cat /etc/timeclock/clock.env
  sudo stat -c "%a %U %n" /etc/timeclock/clock.env
  sudo -n true && echo "sudo without password: OK"
  sudo sshd -T | grep -E "^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) "
  systemctl is-enabled sleep.target suspend.target 2>&1
  sudo passwd -S root
  sudo cat /root/.ssh/authorized_keys
  sudo cat /etc/coolify-tunnel/known_hosts
  sudo ls -lR /data/timeclock_system_files
  sudo ls /root/timeclock-setup 2>&1
  sudo docker version --format "docker {{.Server.Version}}"
  docker compose version
  systemctl is-active docker avahi-daemon ssh systemd-timesyncd
  dpkg-query -W -f="\${Package} \${Status}\n" fprintd libinput-tools python3-gi 2>&1
  dpkg-query -W -f="\${Package} \${Status}\n" network-manager wpasupplicant firmware-realtek firmware-iwlwifi firmware-mediatek firmware-atheros
  systemctl is-active NetworkManager
  nmcli -t -f DEVICE,TYPE,STATE dev | grep -Ev "^(veth|br-|docker)"
  timedatectl | grep -E "Time zone|NTP service"
  apt-config dump | grep -E "Automatic-Reboot|Origins-Pattern"
'
$SSH 'sudo poweroff' 2>/dev/null || true
wait $QEMU 2>/dev/null || true
echo "== done"
