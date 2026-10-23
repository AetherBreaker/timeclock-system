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

# The Coolify tunnel helper (docs/dev-device-setup.md, step 5).
install -m 755 "$S/coolify-tunnel-setup" /usr/local/sbin/coolify-tunnel-setup
