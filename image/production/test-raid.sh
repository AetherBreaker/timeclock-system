#!/bin/sh
# The two-drive (RAID1) install under UEFI (OVMF), with NVMe drives like the HP's; nothing but the clock's name and
# number on the kernel command line. The drives hold old systems as the HP's did: d1 an old Linux, d2 an old Windows,
# each with its boot loader on an EFI partition where the new one goes; d3, which the install doesn't use, a Windows
# EFI partition for os-prober to find; and the firmware has boot entries for all three, plus a USB stick's. Phases:
#   A: the install, which must ask nothing.
#   B: boot with all drives; check the identity, the arrays, both EFI partitions (fresh, synced), the sync hook, that
#      GRUB has no other-OS entries and keeps os-prober off, and that only the stale firmware entries are gone.
#   C: boot with only the second drive; it must come up on its own (degraded).
# Mounts: /out (the ISO), /work, /keys/id (the workstation's private key). Needs --privileged (loop devices, md and
# mounts, to write the old systems) and python3-virt-firmware. Software emulation: slow.
set -eu
cd /work
ISO=/out/$(ls /out | grep '^timeclock-debian-.*\.iso$' | head -n1)
rm -rf vmr && mkdir vmr
xorriso -osirrox on -indev "$ISO" -extract /install.amd/vmlinuz vmr/vmlinuz \
  -extract /install.amd/initrd.gz vmr/initrd.gz >/dev/null 2>&1
install -m 600 /keys/id vmr/id
CODE=/usr/share/OVMF/OVMF_CODE_4M.fd
QEMU="qemu-system-x86_64 -m 2048 -smp 2 -accel tcg -nographic -machine q35
  -drive if=pflash,format=raw,readonly=on,file=$CODE -drive if=pflash,format=raw,file=vmr/vars.fd"
# Fixed PCI slots, as drives stay in their bays: a drive that moves gets its OVMF boot entry re-made behind the EFI
# shell, which then starts instead.
D1="-drive file=vmr/d1.qcow2,if=none,id=d1,format=qcow2 -device nvme,serial=TCDISK0001,drive=d1,addr=0x5"
D2="-drive file=vmr/d2.qcow2,if=none,id=d2,format=qcow2 -device nvme,serial=TCDISK0002,drive=d2,addr=0x6"
D3="-drive file=vmr/d3.qcow2,if=none,id=d3,format=qcow2 -device nvme,serial=TCDISK0003,drive=d3,addr=0x7"
NET="-netdev user,id=n0,hostfwd=tcp::2222-:22 -device virtio-net-pci,netdev=n0"
clean() { sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' "$1" | tr -cd '\12\40-\176'; }
part() {  # $1 image, $2 partition number: a loop device over that partition
  set -- "$1" $(sfdisk -d "$1" | sed -n "s|^$1$2 : start= *\([0-9]*\), size= *\([0-9]*\).*|\1 \2|p")
  losetup -f --show -o $(($2 * 512)) --sizelimit $(($3 * 512)) "$1"
}
esp() {  # $1 image, $2 partition number, $3...: boot loader paths to leave on it
  L=$(part "$1" "$2"); mkfs.vfat -F 32 "$L" >/dev/null; mkdir -p /mnt/esp; mount "$L" /mnt/esp; shift 2
  for f in "$@"; do mkdir -p "/mnt/esp/${f%/*}"; echo old > "/mnt/esp/$f"; done
  umount /mnt/esp; losetup -d "$L"
}
ESP1=11111111-1111-4111-8111-111111111111 ESP2=22222222-2222-4222-8222-222222222222
ESP3=33333333-3333-4333-8333-333333333333
MSR=E3C9E316-0B5C-4DB8-817D-F92DF00215AE DATA=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7

echo "== old systems on the drives ($(date -u +%H:%M))"
rm -f /tmp/d1.raw /tmp/d2.raw /tmp/d3.raw
truncate -s 20G /tmp/d1.raw /tmp/d2.raw
truncate -s 2G /tmp/d3.raw
# d1: a Linux (ubuntu): EFI partition, swap, and one half of an ext4 RAID1.
printf 'label: gpt\nsize=512M, type=U, uuid=%s\nsize=1G, type=S\ntype=R\n' $ESP1 | sfdisk -q /tmp/d1.raw
esp /tmp/d1.raw 1 EFI/ubuntu/shimx64.efi EFI/ubuntu/grubx64.efi EFI/BOOT/BOOTX64.EFI
L=$(part /tmp/d1.raw 2); mkswap "$L" >/dev/null; losetup -d "$L"
L=$(part /tmp/d1.raw 3)
mdadm --create /dev/md100 --run --level=1 --raid-devices=2 --metadata=1.2 "$L" missing 2>/dev/null
mkfs.ext4 -q /dev/md100; mdadm --stop /dev/md100 2>/dev/null; losetup -d "$L"
# d2: Windows: EFI partition (100 MB, as Windows makes it), Microsoft reserved, basic data (FAT standing in for NTFS).
printf 'label: gpt\nsize=100M, type=U, uuid=%s\nsize=16M, type=%s\ntype=%s\n' $ESP2 $MSR $DATA | sfdisk -q /tmp/d2.raw
esp /tmp/d2.raw 1 EFI/Microsoft/Boot/bootmgfw.efi EFI/Microsoft/Boot/BCD EFI/BOOT/BOOTX64.EFI
L=$(part /tmp/d2.raw 3); mkfs.vfat -F 32 "$L" >/dev/null; losetup -d "$L"
# d3: another Windows EFI partition, on a drive the install leaves alone.
printf 'label: gpt\nsize=100M, type=U, uuid=%s\n' $ESP3 | sfdisk -q /tmp/d3.raw
esp /tmp/d3.raw 1 EFI/Microsoft/Boot/bootmgfw.efi EFI/Microsoft/Boot/BCD
for d in d1 d2 d3; do sfdisk -l -o Device,Size,Type /tmp/$d.raw | sed -n '/^\/tmp/p'; done
for d in d1 d2 d3; do qemu-img convert -O qcow2 /tmp/$d.raw vmr/$d.qcow2; rm /tmp/$d.raw; done

# Their firmware boot entries (stale Windows and ubuntu ones, one for d3's existing Windows, a USB stick's), as
# EFI_LOAD_OPTIONs: HD() and File() device path nodes, and a PciRoot/Pci/USB prefix for the stick.
python3 - $ESP1 $ESP2 $ESP3 > vmr/boot.json <<'EOF'
import json, struct, sys, uuid
esp1, esp2, esp3 = sys.argv[1:]
def node(t, s, body): return struct.pack('<BBH', t, s, 4 + len(body)) + body
def hd(n, guid, start, size): return node(4, 1, struct.pack('<IQQ', n, start, size) + uuid.UUID(guid).bytes_le + b'\2\2')
def file(path): return node(4, 4, (path + '\0').encode('utf-16-le'))
usb = node(2, 1, struct.pack('<II', 0x0A0341D0, 0)) + node(1, 1, bytes([0, 0x14])) + node(3, 5, bytes([3, 0]))
entries = {
    0x10: ('Windows Boot Manager', hd(1, esp2, 2048, 204800) + file(r'\EFI\Microsoft\Boot\bootmgfw.efi')),
    0x11: ('ubuntu', hd(1, esp1, 2048, 1048576) + file(r'\EFI\ubuntu\shimx64.efi')),
    0x12: ('Windows Boot Manager', hd(1, esp3, 2048, 204800) + file(r'\EFI\Microsoft\Boot\bootmgfw.efi')),
    0x13: ('USB stick', usb + hd(2, str(uuid.uuid4()), 452, 11520) + file(r'\EFI\BOOT\BOOTX64.EFI')),
}
guid = '8be4df61-93ca-11d2-aa0d-00e098032b8c'
out = []
for num, (title, path) in entries.items():
    path += b'\x7f\xff\4\0'
    data = struct.pack('<IH', 1, len(path)) + (title + '\0').encode('utf-16-le') + path
    out.append({'name': 'Boot%04X' % num, 'guid': guid, 'attr': 7, 'data': data.hex()})
out.append({'name': 'BootOrder', 'guid': guid, 'attr': 7, 'data': b''.join(struct.pack('<H', n) for n in entries).hex()})
json.dump({'version': 2, 'variables': out}, sys.stdout)
EOF
virt-fw-vars -i /usr/share/OVMF/OVMF_VARS_4M.fd -o vmr/vars.fd --set-json vmr/boot.json >/dev/null 2>&1
virt-fw-vars -i vmr/vars.fd -p 2>&1 | grep '^Boot'
set +e

echo "== A: install ($(date -u +%H:%M))"
timeout 5h $QEMU $D1 $D2 $D3 $NET -no-reboot -cdrom "$ISO" -kernel vmr/vmlinuz -initrd vmr/initrd.gz \
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

echo "== B: all drives ($(date -u +%H:%M))"
if boot_and_wait "$D1 $D2 $D3" c; then
  $SSH '
    set -x
    hostname
    sudo cat /etc/timeclock/clock.env
    cat /proc/mdstat
    lsblk -o NAME,SIZE,TYPE,FSTYPE,PARTUUID,MOUNTPOINTS
    findmnt /boot/efi; findmnt /boot/efi2
    sudo find /boot/efi /boot/efi2 -type f
    sudo diff -r /boot/efi /boot/efi2 && echo "EFI partitions identical"
    cat /etc/apt/apt.conf.d/90-sync-esp; grep efi2 /etc/fstab
    sudo grep -A3 "os-prober call found" /var/log/installer/syslog
    grep -n OS_PROBER /etc/default/grub /etc/default/grub.d/* 2>&1
    sudo grep -c -iE "osprober|windows" /boot/grub/grub.cfg
    sudo grep "^menuentry" /boot/grub/grub.cfg
    sudo grep "deleting firmware boot entry" /var/log/installer/syslog
    sudo efibootmgr -v
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
