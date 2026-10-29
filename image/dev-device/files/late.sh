#!/bin/sh
# Runs inside the installed system (in-target) at the end of the install.
set -eu
S=/root/timeclock-setup

# Development SSH access for tcadmin, by key only.
install -d -m 700 -o tcadmin -g tcadmin /home/tcadmin/.ssh
install -m 600 -o tcadmin -g tcadmin "$S/authorized_keys" /home/tcadmin/.ssh/authorized_keys
install -m 644 "$S/10-timeclock.conf" /etc/ssh/sshd_config.d/10-timeclock.conf

# Root logs in by key only (Coolify); its authorized_keys is added after Coolify makes the key.
install -d -m 700 /root/.ssh

# Passwordless sudo on this test device, so tests can run over SSH non-interactively.
printf 'tcadmin ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/90-tcadmin
chmod 440 /etc/sudoers.d/90-tcadmin
visudo -cf /etc/sudoers.d/90-tcadmin

# A clock never sleeps.
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# Wifi beside the wired port (preseed installs NetworkManager, its connectivity check and the firmware): the check every
# minute, and the network baked in at build time, if any, on any wifi device. Both links stay up; NetworkManager routes
# over the wired port (metric 100 against wifi's 600) and moves to wifi when the port loses its link or its internet.
# The setup folder stays on this image, so the password's only copy is NetworkManager's root-only profile.
install -m 644 "$S/30-connectivity-interval.conf" /etc/NetworkManager/conf.d/30-connectivity-interval.conf
[ ! -f "$S/wifi.nmconnection" ] || install -m 600 "$S/wifi.nmconnection" /etc/NetworkManager/system-connections/wifi.nmconnection
rm -f "$S/wifi.nmconnection"
install -m 644 "$S/rtw88-usb.conf" /etc/modprobe.d/rtw88-usb.conf

# The Coolify tunnel helper (docs/dev-device-setup.md, step 5).
install -m 755 "$S/coolify-tunnel-setup" /usr/local/sbin/coolify-tunnel-setup

# RAID1: partman mounts only the first drive's EFI partition. Mount the second at /boot/efi2 and
# keep it a copy (sync-esp, run by apt after every package change), so either drive boots alone.
# Its partition is #1 on the same drive as each RAID member of / (partition #3, timeclock-raid.recipe).
install -m 755 "$S/sync-esp" /usr/local/sbin/sync-esp
install -m 644 "$S/90-sync-esp" /etc/apt/apt.conf.d/90-sync-esp
install -m 644 "$S/52timeclock-unattended" /etc/apt/apt.conf.d/52timeclock-unattended
root_dev=$(findmnt -no SOURCE /)
main_esp=$(findmnt -no SOURCE /boot/efi || true)
case "$root_dev" in /dev/md*)
  for member in $(mdadm --detail "$root_dev" | awk '/active sync/ {print $NF}'); do
    esp=${member%3}1
    [ "$esp" = "$main_esp" ] && continue
    [ "$(blkid -s TYPE -o value "$esp" || true)" = vfat ] || mkfs.vfat -F 32 -n EFI2 "$esp"
    printf 'UUID=%s /boot/efi2 vfat umask=0077,nofail 0 1\n' "$(blkid -s UUID -o value "$esp")" >> /etc/fstab
    install -d /boot/efi2
    mount "$esp" /boot/efi2
    /usr/local/sbin/sync-esp
    umount /boot/efi2   # fstab mounts it from the first boot on; leaving it would block the installer's unmount
  done ;;
esac

# Drop "Windows Boot Manager" entries whose partition this install erased. HP firmware keeps
# trying to restore a dead one (its "boot option restoration" loop) instead of booting Debian.
if [ -d /sys/firmware/efi ]; then
  efibootmgr -v | sed -n 's/^Boot\([0-9A-Fa-f]\{4\}\)\* Windows Boot Manager.*HD([0-9]*,GPT,\([0-9a-fA-F-]*\),.*/\1 \2/p' |
    while read -r num partuuid; do
      blkid -t PARTUUID="$partuuid" >/dev/null 2>&1 || efibootmgr -q -b "$num" -B
    done
fi
