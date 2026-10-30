# Setting up the first test device

The test clock: an HP EliteDesk 800 G3 (or similar) running Debian 13 with no desktop, reachable two ways:

- **On the LAN**, by SSH, for development and testing (`ssh timeclock-dev`).
- **From Coolify on the VPS**, through a persistent reverse SSH tunnel to [coolify-tunnel](https://github.com/AetherBreaker/coolify-tunnel) (requirements, Decided 29). Coolify then installs Docker and deploys to it.

The installer image does most of the setup. Its source is [`image/dev-device/`](../image/dev-device/).

## What the image sets up

- With two internal drives: mirrored (Linux software RAID1 for `/` and swap), with an EFI partition on each drive, kept identical by `sync-esp` after every package change, so either drive boots alone. With one drive: a plain install. Set the BIOS storage controller to AHCI (not Intel RAID).
- Debian 13.7, no desktop. Hostname asked during the install (`timeclock-dev` is the test device's), announced on the LAN as `<hostname>.local`.
- User `tcadmin`, password `timeclock` (console only; change it with `passwd`). Passwordless `sudo`, so tests can run over SSH.
- Root has no password. It can log in over SSH by key only, which is how Coolify connects.
- SSH by key only. `tcadmin` accepts the workstation key `~/.ssh/timeclock_dev`, and `~/.ssh/config` already has a `timeclock-dev` entry for it.
- Time zone `America/New_York`, clock synced over NTP. Sleep and suspend disabled.
- Security updates installed automatically (`unattended-upgrades`, Debian-Security only), rebooting at 03:00 when one needs it.
- Hardware-testing tools: `usbutils`, `libinput-tools`, `fprintd`, and libfprint's Python bindings (`gir1.2-fprint-2.0`, `python3-gi`).
- `coolify-tunnel-setup`, which sets up the tunnel for Coolify (step 5).
- The clock's display ([timeclock-standby](https://github.com/AetherBreaker/timeclock-standby),
  [design](superpowers/specs/2026-10-29-standby-screen-design.md)): a splash with the logo from power-on and no boot
  menu, then sway on terminal 7 showing the clock (cog on the app, at `127.0.0.1:8080`) or, whenever the clock can't
  show (booting, a deploy), a standby page with live steps; the screen watcher picks which. Remote access (RustDesk)
  runs on the host and sees both. See [Servicing](#servicing) for the consoles.

## 1. Write the USB stick

Use `image/dev-device/out/timeclock-dev-debian-13.7.0-amd64.iso`. Check it against its `.sha256` file if you like. Write it with **Rufus** (choose **DD image mode** when asked) or **balenaEtcher**. Everything on the stick is erased.

## 2. BIOS

Press **F10** at power-on.

1. **Advanced → Boot Options:** UEFI boot on; USB boot on.
2. **Advanced → Power-On Options → After Power Loss: Power On**, so the clock comes back by itself after an outage.
3. Leave Secure Boot as it is; Debian boots with it on.
4. If the clock drifts or resets while unplugged, replace the CMOS battery (CR2032).

## 3. Install

1. Plug in **wired** network, the fingerprint reader and the touchscreen.
2. Boot from the stick: press **F9** at power-on and pick the USB stick.
3. At the menu, **Timeclock dev install** is already selected: press **Enter**. The menu has no timeout, so a stick left plugged in can't start a reinstall on its own.
4. The installer asks for the **hostname** (`timeclock-dev` for the test device), then runs by itself and stops at **"Write the changes to disks?"**. With one internal drive, nothing has been touched yet: check that it names that drive, not the USB stick, then choose **Yes**. With two (the EliteDesk's two 500 GB NVMe drives), it builds the mirror on both before asking, so both are already repartitioned by then and pressing Enter in step 3 was the point of no return; choose **Yes** at each prompt. Everything on the internal drives is erased.
5. When it reboots, remove the stick.

## 4. First checks

From your workstation, in PowerShell:

```powershell
ssh timeclock-dev
```

If `timeclock-dev.local` doesn't resolve, log in at the console (`tcadmin` / `timeclock`), run `ip -4 addr`, and use the address instead.

On the device:

```sh
lsusb | grep -i 05ba     # the U.are.U 4500: "ID 05ba:000a DigitalPersona, Inc. ..."
lsusb                    # the touchscreen's controller should also be listed
sudo libinput list-devices | grep -A3 -i touch
```

If the reader shows a different ID, stop and tell me: libfprint's driver only recognises certain IDs.

A quick reader test: `fprintd-enroll`, then `fprintd-verify`. Afterwards, remove any test enrollment with `fprintd-delete tcadmin`.

## 5. The tunnel for Coolify

The VPS end is [coolify-tunnel](https://github.com/AetherBreaker/coolify-tunnel), a forwarding-only SSH container shared by every project's devices. Each device dials it on port 2223 and forwards one port there to its own SSH server. Coolify reaches the device at `coolify-tunnel:<port>` over the `coolify` network. Nothing is configured on the VPS host itself.

### 5a. Deploy coolify-tunnel (once, for all devices)

Follow [its README's "Deploying"](https://github.com/AetherBreaker/coolify-tunnel#deploying). Its host key pair is already made, in the `coolify-tunnel` checkout's root (gitignored). From its log, note the line `coolify-tunnel host key: 256 SHA256:...`.

Port 2223 is published by Docker, which bypasses the VPS's `ufw`, so no rule is needed there. A firewall in the hosting provider's panel would still need to allow it.

### 5b. On the device

```sh
sudo coolify-tunnel-setup              # defaults: port 22001, VPS tunnels.sweetfiretobacco.com, tunnel port 2223
# or: sudo coolify-tunnel-setup <port> <vps> <tunnel-port>
```

The script:

1. Creates the device's tunnel key.
2. Shows the tunnel's host key fingerprint. Answer `y` only if it matches the one from the container's log (5a).
3. Starts the `coolify-tunnel` service.
4. Prints one line for `docker/authorized_keys` in coolify-tunnel. Add it with a comment naming the device and project, commit, push, and redeploy coolify-tunnel in Coolify.

The service retries every 10 seconds, so it connects once the redeploy is done. The next clock uses tunnel port `22002`.

### 5c. In Coolify

1. **Keys & Tokens → Private Keys:** create a key for this device, and copy its public key.
2. On the device, authorize it for root:

   ```sh
   echo '<coolify public key>' | sudo tee -a /root/.ssh/authorized_keys >/dev/null
   sudo chmod 600 /root/.ssh/authorized_keys
   ```

3. **Servers → Add:** name `timeclock-dev`, IP `coolify-tunnel`, port `22001`, user `root`, the key from step 1. **Validate**: Coolify checks the connection and installs Docker.
4. On the server's **Proxy** page, choose **None** (requirements, Decided 14.5).

Coolify renames the container (`coolify-tunnel-<id>`) but keeps `coolify-tunnel` as its alias on the `coolify` network, and the server form accepts that name: `timeclock-dev` validated this way (2026-10-26).

**After reinstalling a device**, its tunnel key and SSH host key are new: rerun `coolify-tunnel-setup`, replace the device's line in coolify-tunnel's `docker/authorized_keys` and redeploy, put Coolify's public key back in `/root/.ssh/authorized_keys`, and validate the server again in Coolify (it reinstalls Docker). If it refuses, use the `coolify-tunnel` container's IP on the `coolify` network: `docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' coolify-tunnel` on the VPS.

## 6. Afterwards

- **Wifi** (built in, or a USB dongle such as the TP-Link Archer T3U): the device joins the network the image was built
  with (`WIFI_SSID`, below) on whichever wifi device it has. Wired and wifi stay up together: traffic goes over the
  cable, and moves to wifi when the cable loses its link or its internet (checked every minute). A Realtek USB dongle
  stays in USB 2 mode on any port, since USB 3's noise drowns 2.4 GHz wifi. For another network,
  over SSH while wired: `sudo nmcli dev wifi connect "<network>" password "<password>"`. It reconnects after every
  reboot; `nmcli dev` shows the state.
- To run Docker as `tcadmin` once Coolify has installed it: `sudo usermod -aG docker tcadmin`, then log in again.
- Before the time clock app is deployed here, make sure nothing is using the reader: fprintd only claims it while a command like `fprintd-verify` is running.

## Servicing

- **A console at the clock:** Ctrl+Alt+F2 (to F6) gives a login prompt. The screen watcher leaves it alone until
  2 minutes pass with no keyboard or touch input, then puts back the screen; Ctrl+Alt+F7 returns to it at once. A
  console login logs out by itself after 5 minutes idle.
- **The boot menu:** hold Esc while the clock boots.
- **The standby page from the workstation:** `ssh -L 8091:127.0.0.1:8091 timeclock-system-<name>`, then
  `http://localhost:8091/`.
- **Settings:** `/etc/timeclock/watcher.conf` (the screen watcher; `flip = deploy-start` shows the standby as soon as
  a deploy starts, not when the old app stops) and `/etc/timeclock/standby.conf` (the status app); every key and its
  default is in each file. Restart `timeclock-watcher` or `timeclock-status` after a change.
- **The display's parts** are systemd units: `timeclock-display` (sway), `timeclock-standby` and `timeclock-clock`
  (the two browsers), `timeclock-status`, `timeclock-watcher`, and `sweetfiresupportdesk` (RustDesk). `journalctl -u
  <unit>` for each; restarting `timeclock-clock` reloads the clock with nobody seeing it (it's hidden until live).
- **Updating the display on a clock:** replace a file under `/opt/timeclock-standby/` over SSH and restart its unit,
  or unpack a newer timeclock-standby release and run its `install.sh` as root (safe to run again; it keeps edited
  settings).

## Deploying the clock

Once per clock, after the install. timeclock-dev's values are in parentheses.

1. **Tunnel:** `sudo coolify-tunnel-setup <port>` with the next free port (timeclock-dev has 22001, so clocks start at 22002), add its line to coolify-tunnel's `docker/authorized_keys`, and redeploy coolify-tunnel (5b).
2. **Coolify server:** at `coolify-tunnel:<port>`, with Coolify's key in root's `authorized_keys` and the proxy set to **None** (5c).
3. **Hub:** in wireguard-hub's `peers.toml`, a row `timeclock-system-<INSTANCE_ID>` (`timeclock-system-dev`) with the next free `/32` and `lan = "office"` (`lan = "dev"`); in `rules.v4`, forwards from that address to the log server (TCP 9020) and the backend (TCP 5432); then a hub release.
4. **Database login,** in the SQL editor: `CREATE ROLE timeclock_clock_<INSTANCE_ID> LOGIN PASSWORD '<random>' IN ROLE timeclock_clocks;` (`timeclock_clock_dev` … `IN ROLE timeclock_test_clocks`).
5. **On the device:** `sudo mkdir -p /data/timeclock_system_files/branding`, and copy `SFT-Logo-June-2022-color.svg` into `branding/`. Without it the screen shows no logo.
   For remote access, also `sudo install -d -m 700 /data/timeclock_system_files/remote` and copy the Sweet Fire Support Desk client into it as `sweetfiresupportdesk.deb` (`sudo install -m 600 …`); it's kept out of git, since its password is baked in. The display installs it at every boot when it changed, and keeps its settings, and so its ID, in `remote/config/`. Without it the clock runs without remote access.
6. **Coolify application** (Docker Compose, this repository, `docker/compose.yaml`) with these variables, then deploy. Deploys stop the old containers first, which blanks the screen and stops punches, so deploy outside shift changes.

   | Variable | Value |
   | --- | --- |
   | `INSTANCE_ID` | names the clock and its location in alerts, logs and the database (`dev`) |
   | `CLOCK_NUMBER` | a whole number from 1 to 99, unique per clock: clock *n* numbers the employees it adds n×10000+1 to n×10000+9999 (timeclock-dev: an unused one, e.g. `99`) |
   | `ALERTS_EMAIL_PWD` | as on other projects |
   | `PINGKEY` | optional: the healthchecks.io ping key, as on other projects; empty means no heartbeat ping (timeclock-dev leaves it empty) |
   | `WG_PRIVATE_KEY` | the contents of `timeclock-system-<INSTANCE_ID>.key` in the wireguard-hub checkout (gitignored) |
   | `WG_HUB_URL`, `WG_HUB_REPO`, `WG_HUB_TOKEN` | the hub's fetched-mode values |
   | `TIMECLOCK_DB_NAME` | `timeclock` (`timeclock_test`) |
   | `TIMECLOCK_DB_USER`, `TIMECLOCK_DB_PASSWORD` | the login from step 4 |

   The compose file sets the rest: time zone, log server and database addresses, and alert addresses. The standby page shows until the app serves; then the clock appears.

## Rebuilding the image

The image is built from [`image/dev-device/`](../image/dev-device/) in a `debian:trixie` container (Docker Desktop):

1. `fetch.sh` downloads the current Debian 13 netinst, and verifies it against Debian's signed checksums.
2. `build.sh` adds the answer file (`preseed.cfg.in`) and the setup files (`files/`), and writes the new ISO to `/out`: mount the gitignored `image/dev-device/out/` there. Set `TC_PASSWORD` (tcadmin's password), and `WIFI_SSID` and `WIFI_PASSWORD` (the wifi network the device joins; kept out of git, since the repository is public; unset, the image joins none). It bakes in timeclock-standby's latest release (`fetch-standby.sh`); the installer downloads the latest again when it can and falls back to that copy.
3. `test.sh` installs it in a QEMU VM and checks the result over SSH.
