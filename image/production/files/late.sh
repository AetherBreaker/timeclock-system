#!/bin/sh
# Runs inside the installed system (in-target) at the end of a production clock's install. Everything the clock needs
# before Coolify takes over is set up here; the workstation's new-clock script does the rest (keys, tunnel, Coolify).
set -eu
S=/root/timeclock-setup

# tcadmin: SSH by the workstation's key only (the new-clock script logs in with it), console login by password,
# passwordless sudo so the script runs non-interactively.
install -d -m 700 -o tcadmin -g tcadmin /home/tcadmin/.ssh
install -m 600 -o tcadmin -g tcadmin "$S/authorized_keys" /home/tcadmin/.ssh/authorized_keys
install -m 644 "$S/10-timeclock.conf" /etc/ssh/sshd_config.d/10-timeclock.conf
printf 'tcadmin ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/90-tcadmin
chmod 440 /etc/sudoers.d/90-tcadmin
visudo -cf /etc/sudoers.d/90-tcadmin

# Root logs in by key only, and only Coolify holds it.
install -d -m 700 /root/.ssh
install -m 600 "$S/coolify.pub" /root/.ssh/authorized_keys

# A clock never sleeps.
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# Wifi beside the wired port (preseed installs NetworkManager, its connectivity check and the firmware): the check every
# minute, and the network baked in at build time, if any, on any wifi device. Both links stay up; NetworkManager routes
# over the wired port (metric 100 against wifi's 600) and moves to wifi when the port loses its link or its internet.
install -m 644 "$S/30-connectivity-interval.conf" /etc/NetworkManager/conf.d/30-connectivity-interval.conf
[ ! -f "$S/wifi.nmconnection" ] || install -m 600 "$S/wifi.nmconnection" /etc/NetworkManager/system-connections/wifi.nmconnection
install -m 644 "$S/rtw88-usb.conf" /etc/modprobe.d/rtw88-usb.conf

# The Coolify tunnel helper, with the tunnel's host key already trusted so it runs without asking.
install -m 755 "$S/coolify-tunnel-setup" /usr/local/sbin/coolify-tunnel-setup
install -d -m 700 /etc/coolify-tunnel
install -m 644 "$S/tunnel_known_hosts" /etc/coolify-tunnel/known_hosts

# Docker from Docker's repository (what Coolify installs itself otherwise). Its key was checked at build time.
# The installer still lists the USB stick as a source here (it comments it out after this script), which fails apt-get
# update; drop it now.
sed -i 's/^deb cdrom:/# deb cdrom:/' /etc/apt/sources.list
install -d -m 755 /etc/apt/keyrings
install -m 644 "$S/docker.asc" /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable docker

# The app's persisted folder (docker/compose.yaml): the logo, and the remote-access client the display installs (remote.sh).
D=/data/timeclock_system_files
install -d -m 755 /data "$D" "$D/branding"
install -m 644 "$S/SFT-Logo-June-2022-color.svg" "$D/branding/"
install -d -m 700 "$D/remote"
install -m 600 "$S/sweetfiresupportdesk.deb" "$D/remote/"

# RAID1: partman mounts only the first drive's EFI partition. Mount the second at /boot/efi2 and
# keep it a copy (sync-esp, run by apt after every package change), so either drive boots alone.
# Its partition is #1 on the same drive as each RAID member of / (partition #3, timeclock-raid.recipe).
install -m 755 "$S/sync-esp" /usr/local/sbin/sync-esp
install -m 644 "$S/90-sync-esp" /etc/apt/apt.conf.d/90-sync-esp
install -m 644 "$S/52timeclock-unattended" /etc/apt/apt.conf.d/52timeclock-unattended
root_dev=$(findmnt -no SOURCE /)
main_esp=$(findmnt -no SOURCE /boot/efi || true)
own_esps=$([ -z "$main_esp" ] || blkid -s PARTUUID -o value "$main_esp")
case "$root_dev" in /dev/md*)
  # /boot/efi is on the first drive; nofail so the second boots without it, not into emergency mode.
  sed -i -E 's|^([^#[:space:]]+[[:space:]]+/boot/efi[[:space:]]+[^[:space:]]+[[:space:]]+)([^[:space:]]+)|\1\2,nofail|' /etc/fstab
  for member in $(mdadm --detail "$root_dev" | awk '/active sync/ {print $NF}'); do
    esp=${member%3}1
    [ "$esp" = "$main_esp" ] && continue
    [ "$(blkid -s TYPE -o value "$esp" || true)" = vfat ] || mkfs.vfat -F 32 -n EFI2 "$esp"
    own_esps="$own_esps $(blkid -s PARTUUID -o value "$esp")"
    printf 'UUID=%s /boot/efi2 vfat umask=0077,nofail 0 1\n' "$(blkid -s UUID -o value "$esp")" >> /etc/fstab
    install -d /boot/efi2
    mount "$esp" /boot/efi2
    /usr/local/sbin/sync-esp
    umount /boot/efi2   # fstab mounts it from the first boot on; leaving it would block the installer's unmount
  done ;;
esac

# Drop firmware boot entries left by earlier systems (Windows, ubuntu, an older debian): any on a GPT partition other
# than this install's EFI partitions. HP firmware keeps trying to restore a dead one (its "boot option restoration"
# loop) instead of booting Debian. Firmware built-ins (VenMedia, BBS) and USB devices stay.
if [ -d /sys/firmware/efi ] && [ -n "$own_esps" ]; then
  own_esps=$(echo "$own_esps" | tr A-F a-f)
  efibootmgr -v | grep -v 'USB(' |
    sed -n 's/^Boot\([0-9A-Fa-f]\{4\}\)\*\{0,1\} .*HD([0-9]*,GPT,\([0-9a-fA-F-]*\),.*/\1 \2/p' |
    while read -r num partuuid; do
      case " $own_esps " in
        *" $(echo "$partuuid" | tr A-F a-f) "*) ;;
        *) echo "late.sh: deleting firmware boot entry Boot$num (partition $partuuid)"; efibootmgr -q -b "$num" -B ;;
      esac
    done
fi

# The standby screen, screen watcher and boot splash: after the logo, which the splash shows, and the second EFI
# partition, which install.sh's GRUB update syncs.
sh "$S/install-standby.sh"

# The setup files include the remote-access client; only the copies installed above stay.
rm -rf "$S"
