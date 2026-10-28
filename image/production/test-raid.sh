#!/bin/sh
# The two-drive (RAID1) install under UEFI (OVMF), with two NVMe drives like the HP's, both holding old systems as the
# HP's did; nothing but the clock's name and number on the kernel command line. Three phases:
#   A: the install, which must ask nothing.
#   B: boot with both drives; check the identity, the arrays, both EFI partitions, the sync hook, boot entries.
#   C: boot with only the second drive; it must come up on its own (degraded).
# Mounts: /out (the ISO), /work, /keys/id (the workstation's private key). Needs --privileged (loop devices and md, to
# write the old systems). Software emulation: slow.
set -eu
cd /work
ISO=/out/$(ls /out | grep '^timeclock-debian-.*\.iso$' | head -n1)
rm -rf vmr && mkdir vmr
xorriso -osirrox on -indev "$ISO" -extract /install.amd/vmlinuz vmr/vmlinuz \
  -extract /install.amd/initrd.gz vmr/initrd.gz >/dev/null 2>&1
install -m 600 /keys/id vmr/id
cp /usr/share/OVMF/OVMF_VARS_4M.fd vmr/vars.fd
CODE=/usr/share/OVMF/OVMF_CODE_4M.fd
QEMU="qemu-system-x86_64 -m 2048 -smp 2 -accel tcg -nographic -machine q35
  -drive if=pflash,format=raw,readonly=on,file=$CODE -drive if=pflash,format=raw,file=vmr/vars.fd"
D1="-drive file=vmr/d1.qcow2,if=none,id=d1,format=qcow2 -device nvme,serial=TCDISK0001,drive=d1"
D2="-drive file=vmr/d2.qcow2,if=none,id=d2,format=qcow2 -device nvme,serial=TCDISK0002,drive=d2"
NET="-netdev user,id=n0,hostfwd=tcp::2222-:22 -device virtio-net-pci,netdev=n0"
clean() { sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' "$1" | tr -cd '\12\40-\176'; }
part() {  # $1 image, $2 partition number: a loop device over that partition
  set -- "$1" $(sfdisk -d "$1" | sed -n "s|^$1$2 : start= *\([0-9]*\), size= *\([0-9]*\).*|\1 \2|p")
  losetup -f --show -o $(($2 * 512)) --sizelimit $(($3 * 512)) "$1"
}

echo "== old systems on the drives ($(date -u +%H:%M))"
rm -f /tmp/d1.raw /tmp/d2.raw
truncate -s 20G /tmp/d1.raw /tmp/d2.raw
# d1: a BIOS-booted Linux (MBR, no EFI partition anywhere, so the installer's "Force UEFI?" check fires): a bootable
# ext4, swap, and one half of an ext4 RAID1 that the installer assembles at start.
printf 'label: dos\n,1G,83,*\n,1G,82\n,,fd\n' | sfdisk -q /tmp/d1.raw
L=$(part /tmp/d1.raw 1); mkfs.ext4 -q "$L"; losetup -d "$L"
L=$(part /tmp/d1.raw 2); mkswap "$L" >/dev/null; losetup -d "$L"
L=$(part /tmp/d1.raw 3)
mdadm --create /dev/md100 --run --level=1 --raid-devices=2 --metadata=1.2 "$L" missing 2>/dev/null
mkfs.ext4 -q /dev/md100; mdadm --stop /dev/md100; losetup -d "$L"
# d2: a GPT Windows data disk (Microsoft reserved, a FAT basic-data partition) plus a Linux ext4.
printf 'label: gpt\n,16M,E3C9E316-0B5C-4DB8-817D-F92DF00215AE\n,8G,EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n,,L\n' |
  sfdisk -q /tmp/d2.raw
L=$(part /tmp/d2.raw 2); mkfs.vfat -F 32 "$L" >/dev/null; losetup -d "$L"
L=$(part /tmp/d2.raw 3); mkfs.ext4 -q "$L"; losetup -d "$L"
sfdisk -l -o Device,Size,Type /tmp/d1.raw | tail -n 3; sfdisk -l -o Device,Size,Type /tmp/d2.raw | tail -n 3
qemu-img convert -O qcow2 /tmp/d1.raw vmr/d1.qcow2 && qemu-img convert -O qcow2 /tmp/d2.raw vmr/d2.qcow2
rm -f /tmp/d1.raw /tmp/d2.raw
set +e

echo "== A: install ($(date -u +%H:%M))"
timeout 5h $QEMU $D1 $D2 $NET -no-reboot -cdrom "$ISO" -kernel vmr/vmlinuz -initrd vmr/initrd.gz \
  -append "auto=true priority=critical timeclock/instance-id=test timeclock/clock-number=98 console=ttyS0,115200n8" \
  > vmr/b.log 2>&1 &
QI=$!
# A screen with a dialog's buttons and no output for 5 minutes is a prompt: stop there.
size=0 still=0
while kill -0 $QI 2>/dev/null; do
  sleep 60
  now=$(stat -c %s vmr/b.log)
  if [ "$now" = "$size" ]; then still=$((still + 1)); else still=0; fi
  size=$now
  [ $still -ge 5 ] || continue
  screen=$(tail -c 3000 vmr/b.log | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' | tr -cd '\12\40-\176')
  if echo "$screen" | grep -qE '<(Yes|No|Continue|Go Back)>'; then
    echo "A: stopped at a prompt:"; echo "$screen"; kill $QI; exit 1
  fi
done
wait $QI
echo "A: ended ($(date -u +%H:%M)); last lines:"; clean vmr/b.log | tail -n 8

SSH="ssh -i vmr/id -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o BatchMode=yes tcadmin@localhost"
boot_and_wait() {  # $1: drive args, $2: log name
  $QEMU $1 $NET > "vmr/$2.log" 2>&1 &
  QB=$!
  i=0
  until $SSH true 2>/dev/null; do
    i=$((i + 1)); [ $i -lt 120 ] || { echo "no SSH after 20 min"; clean "vmr/$2.log" | tail -n 25; kill $QB; return 1; }
    sleep 10
  done
}

echo "== B: both drives ($(date -u +%H:%M))"
if boot_and_wait "$D1 $D2" c; then
  $SSH '
    set -x
    hostname
    sudo cat /etc/timeclock/clock.env
    cat /proc/mdstat
    lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
    findmnt /boot/efi; findmnt /boot/efi2
    sudo diff -r /boot/efi /boot/efi2 && echo "EFI partitions identical"
    sudo ls /boot/efi/EFI /boot/efi/EFI/BOOT
    cat /etc/apt/apt.conf.d/90-sync-esp; grep efi2 /etc/fstab
    sudo efibootmgr
    sudo apt-get install -y --reinstall -qq shim-signed >/dev/null 2>&1; echo "reinstall exit $?"
    sudo diff -r /boot/efi /boot/efi2 && echo "EFI partitions identical after apt"
    systemctl is-active docker ssh
  '
  $SSH 'sudo poweroff' 2>/dev/null; wait $QB 2>/dev/null
fi

echo "== C: second drive only ($(date -u +%H:%M))"
if boot_and_wait "$D2" d; then
  $SSH 'set -x; hostname; cat /proc/mdstat; findmnt /; sudo efibootmgr | head -n 3'
  $SSH 'sudo poweroff' 2>/dev/null; wait $QB 2>/dev/null
fi
echo "== done ($(date -u +%H:%M))"
