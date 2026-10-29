# Setting up a production clock

Three steps per clock: install from the production image, enrol it in wireguard-hub by hand, then run `new_clock.py`
from the workstation. The image is
built from [`image/production/`](../image/production/), which shares the test device's setup ([dev-device-setup.md](dev-device-setup.md));
the BIOS settings are the same. Unlike the test image it asks nothing about the disks.

## What the image sets up

Everything the test image does except the test tools (fprintd, libinput-tools), plus:

- It asks the **clock name** (e.g. `office`) and **clock number** (1–99, unique per clock), sets the hostname to
  `timeclock-system-<name>`, and saves both in `/etc/timeclock/clock.env`. Neither can change later.
- Nothing of a previous system: the drives it uses are blanked first (so their EFI partitions are made fresh), GRUB
  doesn't probe for other systems, and firmware boot entries for any partition but this install's EFI partitions
  (an old Windows, ubuntu or debian) are deleted. Firmware built-ins and USB entries stay.
- Docker, from Docker's repository.
- NetworkManager and wifi firmware (Intel, Realtek, MediaTek, Atheros), for wifi built in or on a USB dongle. The clock
  joins the wifi network the image was built with (`WIFI_SSID`, below) on whichever wifi device it has. Wired and wifi
  stay up together: traffic goes over the cable, and moves to wifi when the cable loses its link or its internet
  (NetworkManager checks each link every minute). A Realtek USB dongle stays in USB 2 mode on any port, since USB 3's
  noise drowns 2.4 GHz wifi.
- Coolify's key in root's `authorized_keys`, and the tunnel's host key already trusted, so `coolify-tunnel-setup` doesn't ask.
- `/data/timeclock_system_files/` with the logo in `branding/` and the remote-access client in `remote/`.
- `tcadmin` / `timeclock` at the console, SSH by the workstation's key (`~/.ssh/timeclock_dev`), passwordless sudo.
  The workstation's `~/.ssh/config` has one entry for every clock, so `ssh timeclock-system-<name>` reaches any of them:
  `Host timeclock-system-*` with `HostName %h.local`, `User tcadmin`, `IdentityFile ~/.ssh/timeclock_dev` and
  `IdentitiesOnly yes`.

The ISO holds the remote-access client, so it stays in the gitignored `image/production/out/`.

## Once: a Coolify API token

In Coolify, **Keys & Tokens → API tokens**, create one with **root** permission, and add it to this repository's
`.env` as `COOLIFY_API_TOKEN=...`.

## Once: Coolify's ssh rule

Coolify reaches each clock at `<hostname>-tunnel:<port>`: it allows only one server per address whatever the port, and
an ssh rule mounted into Coolify's own container sends every `*-tunnel` name to coolify-tunnel. Set up per the
coolify-tunnel README, "Coolify's ssh rule".

## Per clock

1. **Write the stick** with `image/production/out/timeclock-debian-<version>-amd64.iso` (Rufus in DD image mode, or balenaEtcher).
2. **BIOS** as for the test device (dev-device-setup.md, 2).
3. **Install:** wired network plugged in; boot the stick (F9); **Timeclock install** is selected, press Enter. Enter the
   clock name and number; that is the last chance to stop. After them it erases every internal drive (two drives: a
   RAID1 mirror) and installs without asking anything more. Remove the stick when it reboots.
4. **Enrol it in wireguard-hub, by hand** (never from a script, since it ends in a release), in the wireguard-hub
   checkout:
   - a key pair; the private key, base64, in `timeclock-system-<name>.key` (gitignored);
   - a `[[peers]]` row in `src/wireguard_hub/peers.toml` with the next address (the README lists the taken ones; update
     its list and the file's comment);
   - two forwards in `src/wireguard_hub/rules.v4`, copying timeclock-dev's: to the central log server on 9020 and to
     timeclock-backend on 5432;
   - commit, push, `poe release-and-pin patch Enrol timeclock-system-<name>` (Coolify deploys the hub on that push).
5. **From the workstation**, in this repository:

   ```powershell
   uv run image/production/new_clock.py timeclock-system-<name>.local
   ```

   It checks the hub enrolment against the pinned release (the row, its key, both forwards), opens its Coolify tunnel
   (its key line in coolify-tunnel, which it redeploys), adds and validates the Coolify
   server, creates its database login, then creates the Coolify application: named exactly as the hostname, every
   variable filled in except `PINGKEY`, `ALERTS_PUSHOVER_TOKEN` and `ALERTS_PUSHOVER_USER_KEY`, watch paths
   `docker/**`, on the predefined network. It takes up to twenty minutes, mostly Coolify's validation. If a step fails,
   fix the cause and run it again: done steps are skipped.
6. **In Coolify**, on the new application: set `PINGKEY` (the healthchecks.io ping key; empty means no heartbeat
   ping) and the Pushover pair if wanted, turn **Advanced → Container → Consistent Container Names** on (the API can't
   set it), then **Deploy**.

   The variables stay editable in the application's settings in Coolify.
7. **A different wifi network** than the image's: over SSH while still wired,
   `sudo nmcli dev wifi connect "<network>" password "<password>"`. It reconnects after every reboot. Wired stays the
   better choice for a clock.

## Rebuilding the image

In a `debian:trixie` container (Docker Desktop), with `image/` at `/src` (read-only), `image/production/work` at
`/work`, `image/production/out` at `/out`, `image/production/private` at `/private` (the logo and
`sweetfiresupportdesk.deb`, kept out of git), and the workstation's `timeclock_dev.pub` at `/keys/authorized_keys`:
`dev-device/fetch.sh`, then `production/build.sh`, with `TC_PASSWORD` (tcadmin's password) and `WIFI_SSID` and
`WIFI_PASSWORD` (the wifi network every clock joins; kept out of git, since the repository is public; unset, the image
joins none) set in the container. `production/test.sh` installs the result in a QEMU VM on one blank
drive (with the private key at `/keys/id`) and checks it over SSH; `production/test-raid.sh` (container run with
`--privileged`, and `python3-virt-firmware` for its firmware boot entries) does the two-drive RAID1 install under UEFI
on drives holding old systems with stale boot entries, then boots all drives and the second alone. Both fail if the installer asks anything beyond what the kernel command line answers (the name and
number). Each install takes hours under software emulation.

`files/coolify.pub` is Coolify's public key; `files/tunnel_known_hosts` is coolify-tunnel's host key. Replace either if it changes.
