#!/bin/sh
# The two-drive (RAID1) install under UEFI (OVMF), with two NVMe drives like the HP, in four phases:
#   A: with no confirmations answered, the installer must stop at a disk-write confirmation.
#   B: a full install, the confirmations answered on the kernel command line.
#   C: boot with both drives; check the arrays, both EFI partitions, the sync hook, boot entries.
#   D: boot with only the second drive; it must come up on its own (degraded).
# Mounts: /out (the ISO), /work, /keys/id (the development private key). Software emulation: slow.
set -u
cd /work
ISO=/out/$(ls /out | grep '\.iso$' | head -n1)
rm -rf vmr && mkdir vmr
xorriso -osirrox on -indev "$ISO" -extract /install.amd/vmlinuz vmr/vmlinuz \
  -extract /install.amd/initrd.gz vmr/initrd.gz >/dev/null 2>&1
install -m 600 /keys/id vmr/id
cp /usr/share/OVMF/OVMF_VARS_4M.fd vmr/vars.fd
CODE=/usr/share/OVMF/OVMF_CODE_4M.fd
fresh_disks() { rm -f vmr/d1.qcow2 vmr/d2.qcow2; qemu-img create -q -f qcow2 vmr/d1.qcow2 20G; qemu-img create -q -f qcow2 vmr/d2.qcow2 20G; }
QEMU="qemu-system-x86_64 -m 2048 -smp 2 -accel tcg -nographic -machine q35
  -drive if=pflash,format=raw,readonly=on,file=$CODE -drive if=pflash,format=raw,file=vmr/vars.fd"
D1="-drive file=vmr/d1.qcow2,if=none,id=d1,format=qcow2 -device nvme,serial=TCDISK0001,drive=d1"
D2="-drive file=vmr/d2.qcow2,if=none,id=d2,format=qcow2 -device nvme,serial=TCDISK0002,drive=d2"
NET="-netdev user,id=n0,hostfwd=tcp::2222-:22 -device virtio-net-pci,netdev=n0"
clean() { tr -cd '\12\40-\176' < "$1" | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g'; }

echo "== A: unanswered confirmations ($(date -u +%H:%M))"
fresh_disks
$QEMU $D1 $D2 $NET -cdrom "$ISO" -kernel vmr/vmlinuz -initrd vmr/initrd.gz \
  -append "auto=true priority=critical console=ttyS0,115200n8" > vmr/a.log 2>&1 &
QA=$!
i=0
until clean vmr/a.log | grep -qE 'Write the changes|configure RAID|Write previous changes'; do
  i=$((i + 1)); [ $i -lt 180 ] || { echo "A: no confirmation within 60 min"; break; }
  kill -0 $QA 2>/dev/null || { echo "A: installer exited early"; break; }
  sleep 20
done
clean vmr/a.log | grep -oE '(Write the changes|configure RAID|Write previous changes)[^│]{0,90}' | head -n 3
echo "A: partition tables written before the question? $(qemu-img info vmr/d1.qcow2 | grep 'disk size')"
kill $QA 2>/dev/null; wait $QA 2>/dev/null

echo "== B: install ($(date -u +%H:%M))"
fresh_disks
cp /usr/share/OVMF/OVMF_VARS_4M.fd vmr/vars.fd
timeout 5h $QEMU $D1 $D2 $NET -no-reboot -cdrom "$ISO" -kernel vmr/vmlinuz -initrd vmr/initrd.gz \
  -append "auto=true priority=critical partman/confirm=true partman/confirm_nooverwrite=true partman-md/confirm=true partman-md/confirm_nooverwrite=true console=ttyS0,115200n8" \
  > vmr/b.log 2>&1
echo "B: ended ($(date -u +%H:%M)); last lines:"; clean vmr/b.log | tail -n 8

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

echo "== C: both drives ($(date -u +%H:%M))"
if boot_and_wait "$D1 $D2" c; then
  $SSH '
    set -x
    cat /proc/mdstat
    lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
    findmnt /boot/efi; findmnt /boot/efi2
    sudo diff -r /boot/efi /boot/efi2 && echo "EFI partitions identical"
    sudo ls /boot/efi/EFI /boot/efi/EFI/BOOT
    cat /etc/apt/apt.conf.d/90-sync-esp; grep efi2 /etc/fstab
    sudo efibootmgr
    sudo apt-get install -y --reinstall -qq shim-signed >/dev/null 2>&1; echo "reinstall exit $?"
    sudo diff -r /boot/efi /boot/efi2 && echo "EFI partitions identical after apt"
  '
  $SSH 'sudo poweroff' 2>/dev/null; wait $QB 2>/dev/null
fi

echo "== D: second drive only ($(date -u +%H:%M))"
if boot_and_wait "$D2" d; then
  $SSH 'set -x; cat /proc/mdstat; findmnt /; sudo efibootmgr | head -n 3'
  $SSH 'sudo poweroff' 2>/dev/null; wait $QB 2>/dev/null
fi
echo "== done ($(date -u +%H:%M))"
