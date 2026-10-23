#!/bin/sh
# Print the ISO's boot menus and top-level layout, to write the build's menu edits against.
set -eu
cd /work
ISO=$(cat iso-name)
rm -rf peek && mkdir peek
xorriso -osirrox on -indev "$ISO" -extract /boot/grub peek/grub -extract /isolinux peek/isolinux >/dev/null 2>&1
echo "--- /boot/grub/grub.cfg"; cat peek/grub/grub.cfg
echo "--- isolinux files"; ls peek/isolinux
echo "--- /isolinux/txt.cfg"; cat peek/isolinux/txt.cfg 2>/dev/null || true
echo "--- /isolinux/isolinux.cfg"; cat peek/isolinux/isolinux.cfg
echo "--- top level"; xorriso -indev "$ISO" -ls / 2>/dev/null
echo "--- install.amd"; xorriso -indev "$ISO" -ls /install.amd /install.amd/gtk 2>/dev/null
echo "--- el torito as mkisofs"; xorriso -indev "$ISO" -report_el_torito as_mkisofs 2>/dev/null
