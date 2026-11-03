# Setting up a production clock

A clock runs the [timeclock-host](https://github.com/AetherBreaker/timeclock-host) image (submodule `host/`): Fedora
CoreOS with the clock's display, the standby screen, the boot splash, remote access and Docker, kept up to date by the
clock itself. One install stick serves every clock. Per clock: install from the stick, enrol it in wireguard-hub by hand,
then run `new_clock.py` from the workstation.

## What the stick sets up

- It asks the **clock name** (e.g. `office`) and **clock number** (1–99, unique per clock), sets the hostname to
  `timeclock-system-<name>`, and saves both in `/etc/timeclock/clock.env`. Neither can change later.
- It erases the internal drives (never the stick or another USB drive) and installs onto them: two drives are mirrored
  (RAID1: either boots the clock alone), one runs alone. Firmware boot entries for an earlier system go, and each drive
  gets its own.
- `tcadmin` with the console password the stick was built with, SSH by the workstation's key (`~/.ssh/timeclock_dev`),
  passwordless sudo; Coolify's key for root.
- The wifi network the stick was built with, on any wifi device, beside the cable: traffic goes over the cable and moves
  to wifi when the cable loses its link or its internet.
- At its first boot the clock mirrors its drives, then switches to the host image's `:stable` channel and restarts into
  it (a few minutes, with internet). It says on screen what it's doing, every 15 seconds: the network's state while
  there's no internet, then how much it has downloaded.

## Once

- **The stick:** in the timeclock-host checkout (Docker Desktop running),
  `TC_PASSWORD=... WIFI_SSID=... WIFI_PASSWORD=... install/make-iso.sh` writes `out/timeclock-install.iso`. It holds
  the console password's hash and the wifi password, so it stays in `out/` (gitignored). Rebuild it only when the
  password, the wifi network or the keys change: it installs Fedora CoreOS and then takes the current host image.
  `install/test.sh` installs it in a VM with two drives and checks the result (an hour or more).
- **A Coolify API token** with **root** permission (Coolify, **Keys & Tokens → API tokens**), as
  `COOLIFY_API_TOKEN=...` in timeclock-host's `.env` (gitignored).
- **RustDesk's client** as `private/sweetfiresupportdesk.deb` in timeclock-host (gitignored: its password is baked in).
- **Coolify's ssh rule**: Coolify reaches each clock at `<hostname>-tunnel:<port>`, and an ssh rule mounted into
  Coolify's own container sends every `*-tunnel` name to coolify-tunnel (coolify-tunnel README, "Coolify's ssh rule").
- **The workstation's SSH config**: one entry for all clocks, through their tunnels, and one per clock with its port
  (`new_clock.py` prints it):

  ```text
  Host timeclock-system-*.local
    ProxyCommand none
  Host timeclock-system-*
    ProxyCommand ssh -q Coolify_Server docker run --rm -i --network coolify busybox:musl nc coolify-tunnel %p
    User tcadmin
    IdentityFile ~/.ssh/timeclock_dev
    IdentitiesOnly yes
  ```

## Per clock

1. **Write the stick** with `out/timeclock-install.iso` (Rufus in DD image mode, or balenaEtcher).
2. **BIOS** (F10 at power-on):
   - **Advanced → Boot Options:** UEFI boot on, USB boot on.
   - Legacy support off: the clock starts the UEFI way only, where it keeps its firmware boot entries up to date.
   - **Advanced → Power-On Options → After Power Loss: Power On**, so the clock comes back by itself after an outage.
   - Secure Boot as it is. If the clock drifts or resets while unplugged, replace the CMOS battery (CR2032).
3. **Install:** network cable plugged in; boot the stick (F9). On its screen, enter the clock name and number, check the
   drives it lists, and type `yes` to erase them. The stick can stay in. The clock restarts, sets itself up, and
   restarts once more into the timeclock system: the logo, then the standby screen, which waits for setup.
4. **Enrol it in wireguard-hub, by hand** (never from a script, since it ends in a release), in the wireguard-hub
   checkout:
   - a key pair; the private key, base64, in `timeclock-system-<name>.key` (gitignored);
   - a `[[peers]]` row in `src/wireguard_hub/peers.toml` with the next address (the README lists the taken ones; update
     its list and the file's comment);
   - two forwards in `src/wireguard_hub/rules.v4`, copying an existing clock's: to the central log server on 9020 and
     to timeclock-backend on 5432;
   - commit, push, `poe release-and-pin patch Enrol timeclock-system-<name>` (Coolify deploys the hub on that push).
5. **From the workstation**, in the timeclock-host checkout:

   ```powershell
   uv run install/new_clock.py timeclock-system-<name>.local   # a test clock: --test before the host
   ```

   It checks the clock runs the host image and the hub enrolment against the pinned release, opens its Coolify tunnel
   (its key line in coolify-tunnel, which it redeploys), adds and validates the Coolify server, puts RustDesk's client
   on the clock, creates its database login, then creates the Coolify application: named exactly as the hostname,
   every variable filled in except `PINGKEY`, `ALERTS_PUSHOVER_TOKEN` and `ALERTS_PUSHOVER_USER_KEY`, host updates on
   `:stable` in a 03:00 window, watch path `docker/compose.yaml` (so only a release, whose pin changes that file,
   deploys it), on the predefined network, Consistent Container Names on. It
   takes up to twenty minutes, mostly Coolify's validation. If a step fails, fix the cause and run it again: done steps
   are skipped. Add the SSH config entry it prints.
6. **In Coolify**, on the new application: set `PINGKEY` (the healthchecks.io ping key; empty means no heartbeat ping)
   and the Pushover pair if wanted, then **Deploy**. The variables stay editable in the application's settings.
7. **A different wifi network** than the stick's: over SSH while still wired,
   `sudo nmcli dev wifi connect "<network>" password "<password>"`. It reconnects after every reboot.

## Reinstalling a clock

The same steps. `new_clock.py` drops the clock's old SSH host key (the reinstall makes a new one), keeps its Coolify
application as it is, registers its new tunnel key, validates the server and deploys the application. The clock gets a new RustDesk ID unless its old `remote/config` folder is copied back into
`/data/timeclock_system_files/remote/`.

## Test clocks

A clock's database decides whether it's in production: a clock whose application has
`TIMECLOCK_DB_NAME=timeclock_test` is a test clock, safe to test anything on; a clock on `timeclock` is in production.
The test database's employees are a copy of production's (same IDs and PINs), so anyone can test with their own login.

To make a clock a test clock (or the reverse, with `timeclock` and `timeclock_clocks`):

1. In the SQL editor, a login for it in the test group:
   `CREATE ROLE timeclock_clock_<name>_test LOGIN PASSWORD '<random>' IN ROLE timeclock_test_clocks;` and
   `ALTER ROLE timeclock_clock_<name> NOLOGIN;` for its production one.
2. In Coolify, its `TIMECLOCK_DB_NAME`, `TIMECLOCK_DB_USER` and `TIMECLOCK_DB_PASSWORD`.
3. Over SSH, stop its app and clear its local store, which holds the other database's rows:
   `sudo docker stop <container>`, then `sudo rm /data/timeclock_system_files/timeclock.sqlite3*`.
4. Redeploy it in Coolify.

## Host updates

The image is published as `ghcr.io/aetherbreaker/timeclock-host`, with two channels: `:latest` (every release) and
`:stable` (releases promoted by hand once a clock on `:latest` has run them). Each clock's application has three
variables the clock reads:

| Variable | Values | Empty |
| --- | --- | --- |
| `HOST_UPDATE_CHANNEL` | `latest`, `stable` | the channel the clock already follows |
| `HOST_UPDATE_MODE` | `immediate` (as soon as nobody is using the clock), `window` | `immediate` |
| `HOST_UPDATE_WINDOW` | `HH:MM/<minutes>m`, the clock's local time | `03:00/60m` |

A change takes effect once the application is redeployed. An update restarts the clock behind the splash, which says a
system update is installing; if the new system doesn't come up within ten minutes, the clock rolls back and never
tries that release again. **Promote a release** to `:stable`:

```powershell
docker buildx imagetools create --tag ghcr.io/aetherbreaker/timeclock-host:stable ghcr.io/aetherbreaker/timeclock-host@sha256:<digest>
```

## Servicing

- **A console at the clock:** Ctrl+Alt+F2 (to F6) gives a login prompt (`tcadmin`, the stick's console password). The
  screen watcher leaves it alone until 2 minutes pass with no keyboard or touch input, then puts back the screen;
  Ctrl+Alt+F7 returns to it at once. A console login logs out by itself after 5 minutes idle.
- **The boot menu:** hold Esc while the clock boots.
- **The standby page from the workstation:** `ssh -L 8091:127.0.0.1:8091 timeclock-system-<name>`, then
  `http://localhost:8091/`.
- **Settings:** `/etc/timeclock/watcher.conf` (the screen watcher; `flip = deploy-start` shows the standby as soon as
  a deploy starts, not when the old app stops) and `/etc/timeclock/standby.conf` (the status app); every key and its
  default is in each file. Restart `timeclock-watcher` or `timeclock-status` after a change.
- **The display's parts** are systemd units: `timeclock-display` (sway), `timeclock-standby` and `timeclock-clock`
  (the two browsers), `timeclock-status`, `timeclock-watcher`, and `sweetfiresupportdesk` (RustDesk). `journalctl -u
  <unit>` for each; restarting `timeclock-clock` reloads the clock with nobody seeing it (it's hidden until live).
- **The display's files** are in the host image, so a change ships in a host release. To try one on a test clock,
  `sudo bootc usr-overlay` makes `/usr` writable until the next restart.
