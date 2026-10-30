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
  it (a few minutes, with internet).

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
2. **BIOS** as for the test device ([dev-device-setup.md](dev-device-setup.md), 2).
3. **Install:** network cable plugged in; boot the stick (F9). On its screen, enter the clock name and number, check the
   drives it lists, and type `yes` to erase them. When it says so, remove the stick. The clock restarts, sets itself up,
   and restarts once more into the timeclock system: the logo, then the standby screen, which waits for setup.
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
   uv run install/new_clock.py timeclock-system-<name>.local
   ```

   It checks the clock runs the host image and the hub enrolment against the pinned release, opens its Coolify tunnel
   (its key line in coolify-tunnel, which it redeploys), adds and validates the Coolify server, puts RustDesk's client
   on the clock, creates its database login, then creates the Coolify application: named exactly as the hostname,
   every variable filled in except `PINGKEY`, `ALERTS_PUSHOVER_TOKEN` and `ALERTS_PUSHOVER_USER_KEY`, host updates on
   `:stable` in a 03:00 window, watch paths `docker/**`, on the predefined network, Consistent Container Names on. It
   takes up to twenty minutes, mostly Coolify's validation. If a step fails, fix the cause and run it again: done steps
   are skipped. Add the SSH config entry it prints.
6. **In Coolify**, on the new application: set `PINGKEY` (the healthchecks.io ping key; empty means no heartbeat ping)
   and the Pushover pair if wanted, then **Deploy**. The variables stay editable in the application's settings.
7. **A different wifi network** than the stick's: over SSH while still wired,
   `sudo nmcli dev wifi connect "<network>" password "<password>"`. It reconnects after every reboot.

## Reinstalling a clock

The same steps. Before step 5, `ssh-keygen -R timeclock-system-<name>.local` (the reinstall has a new SSH host key).
`new_clock.py` keeps the clock's Coolify application as it is, registers its new tunnel key, validates the server and
deploys the application. The clock gets a new RustDesk ID unless its old `remote/config` folder is copied back into
`/data/timeclock_system_files/remote/`.

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
