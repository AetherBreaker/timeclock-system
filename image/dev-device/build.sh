#!/bin/sh
# Remaster the verified Debian netinst (fetch.sh) into the timeclock test-device installer.
# Mounts: /src (this folder, read-only), /work (downloads and scratch), /out (the finished ISO),
# /keys/authorized_keys (the development public key). TC_PASSWORD sets tcadmin's password.
set -eu
SRC=/src
cd /work
ISO=$(cat iso-name)
VER=${ISO#debian-}
VER=${VER%-amd64-netinst.iso}
NEW=timeclock-dev-debian-$VER-amd64.iso

rm -rf build
mkdir -p build/x build/timeclock build/initrd

# Setup files the installer copies into the new system.
cp "$SRC"/files/* build/timeclock/
cp /keys/authorized_keys build/timeclock/authorized_keys
sed -i 's/\r$//' build/timeclock/*

# The answer file, with tcadmin's password hashed in, checked by debconf's own parser.
HASH=$(openssl passwd -6 "${TC_PASSWORD:-timeclock}")
sed -e 's/\r$//' -e "s|@PASSWORD_HASH@|$HASH|" "$SRC/preseed.cfg.in" > build/initrd/preseed.cfg
debconf-set-selections -c build/initrd/preseed.cfg
sed 's/\r$//' "$SRC/timeclock-raid.recipe" > build/initrd/timeclock-raid.recipe

xorriso -osirrox on -indev "$ISO" \
  -extract /install.amd/initrd.gz build/x/initrd.gz \
  -extract /install.amd/gtk/initrd.gz build/x/gtk-initrd.gz \
  -extract /boot/grub/grub.cfg build/x/grub.cfg \
  -extract /isolinux/gtk.cfg build/x/gtk.cfg \
  -extract /md5sum.txt build/x/md5sum.txt >/dev/null 2>&1
chmod u+w build/x/*

# The kernel unpacks concatenated cpio archives in order, so appending one adds /preseed.cfg (which
# the installer loads automatically) and /timeclock-raid.recipe (named by preseed's early_command).
(cd build/initrd && printf 'preseed.cfg\ntimeclock-raid.recipe\n' | cpio -H newc -o --quiet | gzip -9) > build/preseed.cpio.gz
cat build/preseed.cpio.gz >> build/x/initrd.gz
cat build/preseed.cpio.gz >> build/x/gtk-initrd.gz

# UEFI (GRUB): our entry first and selected, with no timeout, so a stick left in can't reinstall.
awk '
  !done && /^menuentry --hotkey=g / {
    print "set default=0"
    print "menuentry --hotkey=t '"'"'Timeclock dev install (automated; asks before erasing the disk)'"'"' {"
    print "    set background_color=black"
    print "    linux    /install.amd/vmlinuz auto=true priority=critical vga=788 --- quiet"
    print "    initrd   /install.amd/gtk/initrd.gz"
    print "}"
    done = 1
  }
  { print }
' build/x/grub.cfg > build/x/grub.cfg.new
mv build/x/grub.cfg.new build/x/grub.cfg
grep -q "Timeclock dev install" build/x/grub.cfg

# BIOS (isolinux): the same entry, made the menu default.
{
  printf 'label timeclock\n\tmenu label ^Timeclock dev install (automated; asks before erasing the disk)\n\tmenu default\n'
  printf '\tkernel /install.amd/vmlinuz\n\tappend auto=true priority=critical vga=788 initrd=/install.amd/gtk/initrd.gz --- quiet\n'
  sed -e '/menu default/d' -e 's/^default .*/default timeclock/' build/x/gtk.cfg
} > build/x/gtk.cfg.new
mv build/x/gtk.cfg.new build/x/gtk.cfg

# Keep md5sum.txt true for the files we changed or added.
for pair in initrd.gz:./install.amd/initrd.gz gtk-initrd.gz:./install.amd/gtk/initrd.gz \
            grub.cfg:./boot/grub/grub.cfg gtk.cfg:./isolinux/gtk.cfg; do
  local_file=build/x/${pair%%:*}
  iso_path=${pair#*:}
  sum=$(md5sum "$local_file" | cut -d' ' -f1)
  sed -i "s|^[0-9a-f]*  $iso_path\$|$sum  $iso_path|" build/x/md5sum.txt
done
for f in build/timeclock/*; do
  echo "$(md5sum "$f" | cut -d' ' -f1)  ./timeclock/$(basename "$f")" >> build/x/md5sum.txt
done

rm -f "/out/$NEW"
xorriso -indev "$ISO" -outdev "/out/$NEW" \
  -map build/x/initrd.gz /install.amd/initrd.gz \
  -map build/x/gtk-initrd.gz /install.amd/gtk/initrd.gz \
  -map build/x/grub.cfg /boot/grub/grub.cfg \
  -map build/x/gtk.cfg /isolinux/gtk.cfg \
  -map build/x/md5sum.txt /md5sum.txt \
  -map build/timeclock /timeclock \
  -boot_image any replay 2>&1 | grep -iE 'error|warn|written' || true

cd /out && sha256sum "$NEW" | tee "$NEW.sha256"
