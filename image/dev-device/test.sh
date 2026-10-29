#!/bin/sh
# Install the built ISO into a QEMU VM unattended (software emulation), boot the result, and
# check the configuration over SSH. Only difference from a real install: the disk confirmations and the
# hostname are answered on the kernel command line. Mounts: /out (the ISO), /work, /keys/id (private key).
set -eu
cd /work
ISO=/out/$(ls /out | grep '\.iso$' | head -n1)
rm -rf vm && mkdir vm
xorriso -osirrox on -indev "$ISO" -extract /install.amd/vmlinuz vm/vmlinuz \
  -extract /install.amd/initrd.gz vm/initrd.gz >/dev/null 2>&1
qemu-img create -q -f qcow2 vm/disk.qcow2 16G
install -m 600 /keys/id vm/id

echo "== install ($(date -u +%H:%M))"
timeout 4h qemu-system-x86_64 -m 2048 -smp 2 -accel tcg -nographic -no-reboot \
  -drive file=vm/disk.qcow2,if=virtio -cdrom "$ISO" \
  -kernel vm/vmlinuz -initrd vm/initrd.gz \
  -append "auto=true priority=critical partman/confirm=true partman/confirm_nooverwrite=true netcfg/get_hostname=timeclock-test console=ttyS0,115200n8" \
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
  [ "$(hostname)" = timeclock-test ] && echo "hostname timeclock-test: OK"
  sudo -n true && echo "sudo without password: OK"
  sudo sshd -T | grep -E "^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) "
  systemctl is-enabled sleep.target suspend.target 2>&1
  sudo passwd -S root
  sudo ls -ld /usr/local/sbin/coolify-tunnel-setup /etc/sudoers.d/90-tcadmin /root/.ssh
  dpkg-query -W -f="\${Package} \${Version} \${Status}\n" fprintd libfprint-2-2 gir1.2-fprint-2.0 python3-gi avahi-daemon systemd-timesyncd libinput-tools usbutils unattended-upgrades
  python3 -c "import gi; gi.require_version(\"FPrint\", \"2.0\"); from gi.repository import FPrint; print(\"FPrint import: OK\")"
  systemctl is-active avahi-daemon ssh systemd-timesyncd
  dpkg-query -W -f="\${Package} \${Status}\n" network-manager network-manager-config-connectivity-debian
  sudo /usr/sbin/NetworkManager --print-config | grep -A2 "^\[connectivity\]"
  nmcli -t -f NAME,TYPE con; sudo nmcli -g connection.interface-name,802-11-wireless.ssid con show "$(nmcli -t -f NAME,TYPE con | sed -n "s/:802-11-wireless\$//p")" 2>&1
  sudo ls /root/timeclock-setup
  sudo modprobe -c | grep "^options rtw88_usb"
  timedatectl | grep -E "Time zone|NTP service"
  apt-config dump | grep -E "Automatic-Reboot|Origins-Pattern"
'
$SSH 'sudo poweroff' 2>/dev/null || true
wait $QEMU 2>/dev/null || true
echo "== done"
