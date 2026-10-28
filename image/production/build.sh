#!/bin/sh
# Remaster the verified Debian netinst (dev-device/fetch.sh) into the production clock installer. It shares the
# dev image's setup files and RAID recipe; production/files (its own late.sh) goes over them.
# Mounts: /src (image/, read-only), /work (downloads and scratch), /out (the finished ISO), /private (the logo and the
# remote-access client, kept out of git), /keys/authorized_keys (the workstation's public key, for tcadmin).
# TC_PASSWORD sets tcadmin's password.
set -eu
DEV=/src/dev-device
SRC=/src/production
cd /work
ISO=$(cat iso-name)
VER=${ISO#debian-}
VER=${VER%-amd64-netinst.iso}
NEW=timeclock-debian-$VER-amd64.iso

rm -rf build
mkdir -p build/x build/timeclock build/initrd

# Setup files the installer copies into the new system.
cp "$DEV"/files/* build/timeclock/
cp "$SRC"/files/* build/timeclock/
cp /keys/authorized_keys build/timeclock/authorized_keys
sed -i 's/\r$//' build/timeclock/*
cp /private/SFT-Logo-June-2022-color.svg /private/sweetfiresupportdesk.deb build/timeclock/
# Docker's repository key, checked against the fingerprint Docker publishes.
wget -q -O build/timeclock/docker.asc https://download.docker.com/linux/debian/gpg
gpg --show-keys --with-colons build/timeclock/docker.asc | grep -qx 'fpr:::::::::9DC858229FC7DD38854AE2D88D81803C0EBFCD88:'

# The answer file, with tcadmin's password hashed in, checked by debconf's own parser.
HASH=$(openssl passwd -6 "${TC_PASSWORD:-timeclock}")
sed -e 's/\r$//' -e "s|@PASSWORD_HASH@|$HASH|" "$SRC/preseed.cfg.in" > build/initrd/preseed.cfg
debconf-set-selections -c build/initrd/preseed.cfg
sed 's/\r$//' "$DEV/timeclock-raid.recipe" > build/initrd/timeclock-raid.recipe
sed 's/\r$//' "$SRC/ask.sh" > build/initrd/timeclock-ask.sh
sed 's/\r$//' "$SRC/timeclock.templates" > build/initrd/timeclock.templates

xorriso -osirrox on -indev "$ISO" \
  -extract /install.amd/initrd.gz build/x/initrd.gz \
  -extract /install.amd/gtk/initrd.gz build/x/gtk-initrd.gz \
  -extract /boot/grub/grub.cfg build/x/grub.cfg \
  -extract /isolinux/gtk.cfg build/x/gtk.cfg \
  -extract /md5sum.txt build/x/md5sum.txt >/dev/null 2>&1
chmod u+w build/x/*

# The kernel unpacks concatenated cpio archives in order, so appending one adds /preseed.cfg (which the installer
# loads automatically) and the files its early_commands name.
(cd build/initrd && printf 'preseed.cfg\ntimeclock-raid.recipe\ntimeclock-ask.sh\ntimeclock.templates\n' |
  cpio -H newc -o --quiet | gzip -9) > build/preseed.cpio.gz
cat build/preseed.cpio.gz >> build/x/initrd.gz
cat build/preseed.cpio.gz >> build/x/gtk-initrd.gz

LABEL="Timeclock install (asks the clock's name and number, then erases the internal drives)"
# UEFI (GRUB): our entry first and selected, with no timeout, so a stick left in can't reinstall.
awk -v label="$LABEL" '
  !done && /^menuentry --hotkey=g / {
    print "set default=0"
    print "menuentry --hotkey=t \"" label "\" {"
    print "    set background_color=black"
    print "    linux    /install.amd/vmlinuz auto=true priority=critical vga=788 --- quiet"
    print "    initrd   /install.amd/gtk/initrd.gz"
    print "}"
    done = 1
  }
  { print }
' build/x/grub.cfg > build/x/grub.cfg.new
mv build/x/grub.cfg.new build/x/grub.cfg
grep -q "Timeclock install" build/x/grub.cfg

# BIOS (isolinux): the same entry, made the menu default.
{
  printf 'label timeclock\n\tmenu label ^%s\n\tmenu default\n' "$LABEL"
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
