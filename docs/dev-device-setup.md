# Setting up the first test device

The test clock: an HP EliteDesk 800 G3 (or similar) running Debian 13 with no desktop, reachable two ways:

- **On the LAN**, by SSH, for development and testing (`ssh timeclock-dev`).
- **From Coolify on the VPS**, through a persistent reverse SSH tunnel to [coolify-tunnel](https://github.com/AetherBreaker/coolify-tunnel) (requirements, Decided 29). Coolify then installs Docker and deploys to it.

The installer image does most of the setup. Its source is [`image/dev-device/`](../image/dev-device/).

## What the image sets up

- With two internal drives: mirrored (Linux software RAID1 for `/` and swap), with an EFI partition on each drive, kept identical by `sync-esp` after every package change, so either drive boots alone. With one drive: a plain install. Set the BIOS storage controller to AHCI (not Intel RAID).
- Debian 13.7, no desktop. Hostname `timeclock-dev`, announced on the LAN as `timeclock-dev.local`.
- User `tcadmin`, password `timeclock` (console only; change it with `passwd`). Passwordless `sudo`, so tests can run over SSH.
- Root has no password. It can log in over SSH by key only, which is how Coolify connects.
- SSH by key only. `tcadmin` accepts the workstation key `~/.ssh/timeclock_dev`, and `~/.ssh/config` already has a `timeclock-dev` entry for it.
- Time zone `America/New_York`, clock synced over NTP. Sleep and suspend disabled.
- Hardware-testing tools: `usbutils`, `libinput-tools`, `fprintd`, and libfprint's Python bindings (`gir1.2-fprint-2.0`, `python3-gi`).
- `coolify-tunnel-setup`, which sets up the tunnel for Coolify (step 5).

## 1. Write the USB stick

Use `timeclock-dev-debian-13.7.0-amd64.iso`. Check it against its `.sha256` file if you like. Write it with **Rufus** (choose **DD image mode** when asked) or **balenaEtcher**. Everything on the stick is erased.

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
4. The installer runs by itself and stops at **"Write the changes to disks?"**. With one internal drive, nothing has been touched yet: check that it names that drive, not the USB stick, then choose **Yes**. With two (the EliteDesk's two 500 GB NVMe drives), it builds the mirror on both before asking, so both are already repartitioned by then and pressing Enter in step 3 was the point of no return; choose **Yes** at each prompt. Everything on the internal drives is erased.
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

- To run Docker as `tcadmin` once Coolify has installed it: `sudo usermod -aG docker tcadmin`, then log in again.
- Before the time clock app is deployed here, make sure nothing is using the reader: fprintd only claims it while a command like `fprintd-verify` is running.

## Rebuilding the image

The image is built from [`image/dev-device/`](../image/dev-device/) in a `debian:trixie` container (Docker Desktop):

1. `fetch.sh` downloads the current Debian 13 netinst, and verifies it against Debian's signed checksums.
2. `build.sh` adds the answer file (`preseed.cfg.in`) and the setup files (`files/`), and writes the new ISO.
3. `test.sh` installs it in a QEMU VM and checks the result over SSH.
