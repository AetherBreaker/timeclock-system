#!/bin/sh
# Installer side of the clock's identity, run from the answer file. `ask` (preseed/early_command, before networking)
# asks the clock's name and number, re-asking until valid; values given on the kernel command line (the VM test) are
# kept if valid. `save` (preseed/late_command) writes both to the new system, and the hostname from the name; there
# the workstation's new-clock script reads them to fill in the clock's Coolify app.
set -eu
. /usr/share/debconf/confmodule

case "${1:-}" in
ask)
  debconf-loadtemplate timeclock /timeclock.templates
  # A kernel-command-line answer may predate the template; take it from there directly.
  for q in instance-id clock-number; do
    v=$(tr ' ' '\n' < /proc/cmdline | sed -n "s|^timeclock/$q=||p")
    [ -z "$v" ] || db_set "timeclock/$q" "$v"
  done
  while :; do
    db_get timeclock/instance-id
    name=$RET
    case "$name" in
      "" | -* | *[!a-z0-9-]*) ;;
      *) [ ${#name} -le 46 ] && break ;;
    esac
    db_fset timeclock/instance-id seen false
    db_input critical timeclock/instance-id || true
    db_go || true
  done
  while :; do
    db_get timeclock/clock-number
    number=$RET
    case "$number" in
      "" | 0* | *[!0-9]*) ;;
      *) [ "$number" -le 99 ] && break ;;
    esac
    db_fset timeclock/clock-number seen false
    db_input critical timeclock/clock-number || true
    db_go || true
  done
  ;;
save)
  db_get timeclock/instance-id
  name=$RET
  db_get timeclock/clock-number
  number=$RET
  mkdir -p /target/etc/timeclock
  printf 'INSTANCE_ID=%s\nCLOCK_NUMBER=%s\n' "$name" "$number" > /target/etc/timeclock/clock.env
  chmod 600 /target/etc/timeclock/clock.env
  # The hostname is written here, over whatever the installer chose (its default, or DHCP's): netcfg's questions
  # can't be set from early_command.
  echo "timeclock-system-$name" > /target/etc/hostname
  sed -i "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\ttimeclock-system-$name/" /target/etc/hosts
  ;;
*)
  echo "usage: $0 ask|save" >&2
  exit 2
  ;;
esac
