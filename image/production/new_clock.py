# /// script
# requires-python = ">=3.14"
# dependencies = ["cryptography>=46"]
# ///
"""Enrol a freshly installed production clock, so its first Coolify deploy just works.

Run from timeclock-system once the clock has rebooted from the install:
  uv run image/production/new_clock.py <host>
<host> is the clock on the LAN: timeclock-system-<name>.local, or its IP address.

Every step checks what's already done, so a rerun after a failure carries on where it stopped:
1. the clock's name and number, from /etc/timeclock/clock.env (written by the installer);
2. wireguard-hub: a key pair (the private key kept as timeclock-system-<name>.key in the checkout, gitignored), a
   peers.toml row with the next address and the clock's two rules.v4 forwards, then a hub release-and-pin;
3. coolify-tunnel: the clock's tunnel on the next free port and its line in docker/authorized_keys, pushed and redeployed;
4. Coolify: the server (coolify-tunnel:<port>, root, no proxy), validated;
5. the clock's database login, timeclock_clock_<name> in timeclock_clocks, with a random password;
6. the Coolify application (docker/compose.yaml of this repository, beside timeclock-dev's), its variables filled in:
   the clock's own from the steps above, the shared ones copied from timeclock-dev's application. Then a deploy.
The variables stay editable in Coolify; nothing here sets them again once the application exists.

Needs, in .env: COOLIFY_API_TOKEN (a root token). And: ~/.ssh/timeclock_dev (tcadmin's key), `ssh Coolify_Server`,
the wireguard-hub and coolify-tunnel checkouts beside this one with push access, and poe at ~/.local/bin.
"""

# Standard library imports
import base64
import json
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Third party imports
from cryptography.hazmat.primitives import serialization  # pyright: ignore[reportMissingImports]
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent
HUB = WORKSPACE / "wireguard-hub"
TUNNEL = WORKSPACE / "coolify-tunnel"
POE = Path.home() / ".local/bin/poe"
COOLIFY = "https://coolify.sweetfiretobacco.com/api/v1"
TEMPLATE_INSTANCE = "dev"  # timeclock-dev's application: where the new one goes, and the shared variables' source
LOG_SERVER, BACKEND = "central-log-server", "timeclock-backend"


def run(*cmd: str | Path, cwd: Path | None = None, stdin: str | None = None) -> str:
  """Run a command, returning its stdout; exit with its output if it fails."""
  result = subprocess.run([str(c) for c in cmd], cwd=cwd, input=stdin, capture_output=True, text=True, check=False)
  if result.returncode:
    sys.exit(f"failed ({result.returncode}): {' '.join(map(str, cmd))}\n{result.stdout}{result.stderr}")
  return result.stdout


def env_file_value(key: str) -> str:
  """A value from this repository's .env."""
  for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.startswith(f"{key}="):
      return line.split("=", 1)[1].strip().strip("'\"")
  sys.exit(f"{key} is not in {ROOT / '.env'}")


TOKEN = env_file_value("COOLIFY_API_TOKEN")


def api(method: str, path: str, body: object = None) -> object:
  """Call Coolify's API, returning the decoded JSON; exit on an HTTP error."""
  data = None if body is None else json.dumps(body).encode()
  request = urllib.request.Request(f"{COOLIFY}{path}", data=data, method=method)
  request.add_header("Authorization", f"Bearer {TOKEN}")
  request.add_header("Accept", "application/json")
  request.add_header("User-Agent", "timeclock-new-clock/1")  # Cloudflare refuses urllib's default
  if data is not None:
    request.add_header("Content-Type", "application/json")
  try:
    with urllib.request.urlopen(request, timeout=60) as response:
      raw = response.read()
  except urllib.error.HTTPError as e:
    sys.exit(f"Coolify {method} {path}: HTTP {e.code}\n{e.read().decode(errors='replace')}")
  return json.loads(raw) if raw else None


def git_commit_push(repo: Path, paths: list[str], message: str) -> None:
  """Commit the given paths in repo and push."""
  run("git", "add", "--", *paths, cwd=repo)
  run("git", "commit", "-m", message, cwd=repo)
  run("git", "push", cwd=repo)


def deploy_app_named(name: str) -> None:
  """Start a deploy of the one Coolify application with this name."""
  apps = [a for a in api("GET", "/applications") if a["name"] == name]  # pyright: ignore[reportGeneralTypeIssues]
  if len(apps) != 1:
    sys.exit(f"expected one Coolify application named {name!r}, found {len(apps)}")
  api("GET", f"/deploy?uuid={apps[0]['uuid']}")
  print(f"  deploying {name} in Coolify")


def main() -> None:  # noqa: C901, D103, PLR0912, PLR0915 - one linear checklist, in order on purpose; the module says what it does
  if len(sys.argv) != 2:  # noqa: PLR2004
    sys.exit(__doc__)
  host = sys.argv[1]
  ssh = ["ssh", "-i", Path.home() / ".ssh/timeclock_dev", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
  ssh += ["-o", "StrictHostKeyChecking=accept-new", f"tcadmin@{host}"]

  # 1. The clock's identity.
  clock = dict(line.split("=", 1) for line in run(*ssh, "sudo cat /etc/timeclock/clock.env").split())
  name, number = clock["INSTANCE_ID"], clock["CLOCK_NUMBER"]
  peer = f"timeclock-system-{name}"
  print(f"clock {name!r}, number {number}, at {host}")
  if run(*ssh, "hostname").strip() != peer:
    sys.exit(f"{host} isn't named {peer}")

  apps = api("GET", "/applications")
  existing = [a for a in apps if a["name"] == peer]  # pyright: ignore[reportGeneralTypeIssues]
  if existing:
    sys.exit(f"Coolify already has an application {peer!r}; change it there")
  # The template: the timeclock-system application whose INSTANCE_ID is timeclock-dev's.
  template, template_envs = None, {}
  for app in apps:  # pyright: ignore[reportGeneralTypeIssues]
    if "timeclock-system" in (app.get("git_repository") or "") and app.get("build_pack") == "dockercompose":
      # Each variable also has a preview-deployment copy; only the real ones count.
      envs = {e["key"]: e for e in api("GET", f"/applications/{app['uuid']}/envs") if not e["is_preview"]}  # pyright: ignore[reportGeneralTypeIssues]
      if envs.get("INSTANCE_ID", {}).get("value") == TEMPLATE_INSTANCE:
        template, template_envs = app, envs
        break
  if template is None:
    sys.exit(f"no timeclock-system application with INSTANCE_ID={TEMPLATE_INSTANCE} in Coolify")

  # 2. WireGuard hub.
  for repo in (HUB, TUNNEL):
    run("git", "pull", "--ff-only", cwd=repo)
  peers_path = HUB / "src/wireguard_hub/peers.toml"
  rules_path = HUB / "src/wireguard_hub/rules.v4"
  key_path = HUB / f"{peer}.key"
  if key_path.exists():
    private = X25519PrivateKey.from_private_bytes(base64.b64decode(key_path.read_text(encoding="utf-8").strip()))
  else:
    private = X25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    key_path.write_text(base64.b64encode(raw).decode() + "\n", encoding="utf-8")
  public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
  wg_private = key_path.read_text(encoding="utf-8").strip()

  peers_text = peers_path.read_text(encoding="utf-8")
  rows = dict(re.findall(r'name = "([^"]+)"\s*\npublic_key = "[^"]+"\s*\naddress = "([^"]+)"', peers_text))
  if not {LOG_SERVER, BACKEND} <= rows.keys():
    sys.exit(f"{peers_path} has rows this script can't read (name, public_key, address, in that order)")
  hub_changed = False
  if peer not in rows:
    last = max(int(a.split(".")[3].split("/")[0]) for a in rows.values())
    rows[peer] = f"10.8.0.{last + 1}/32"
    peers_text = peers_text.rstrip("\n") + f'\n\n[[peers]]\nname = "{peer}"\npublic_key = "{public}"\naddress = "{rows[peer]}"\n'
    # Keep the address lists in the comment and README current.
    peers_text = re.sub(r"further peers from \.\d+", f"{peer} {rows[peer]}, further peers from .{last + 2}", peers_text)
    peers_path.write_text(peers_text, encoding="utf-8")
    readme = HUB / "README.md"
    readme.write_text(
      re.sub(
        r"further peers from `\.\d+`",
        f"`{peer}` `10.8.0.{last + 1}`, further peers from `.{last + 2}`",
        readme.read_text(encoding="utf-8"),
      ),
      encoding="utf-8",
    )
    hub_changed = True
  elif f'public_key = "{public}"' not in peers_text:
    sys.exit(f"peers.toml's {peer} row has a different key than {key_path}; fix one of them by hand")
  rules = rules_path.read_text(encoding="utf-8")
  new_rules = ""
  for target, port, why in (
    (LOG_SERVER, 9020, "the central log server (timeclock design 12, R22)"),
    (BACKEND, 5432, "timeclock-backend's database relay (timeclock design 12, R26.1)"),
  ):
    rule = f"-A FORWARD -i wg0 -o wg0 -s {rows[peer]} -d {rows[target]} -p tcp --dport {port} -m conntrack --ctstate NEW -j ACCEPT"
    if rule not in rules:
      new_rules += f"# {peer} to {why}.\n{rule}\n"
  if new_rules:
    head, sep, tail = rules.rpartition("COMMIT")
    rules_path.write_text(head + new_rules + sep + tail, encoding="utf-8")
    hub_changed = True
  if hub_changed:
    git_commit_push(
      HUB,
      ["README.md", "src/wireguard_hub/peers.toml", "src/wireguard_hub/rules.v4"],
      f"feat(peers): enrol {peer}; forward it to logs and the database relay",
    )
    print(f"  hub: {peer} at {rows[peer]}; releasing")
    run(POE, "release-and-pin", "patch", f"Enrol {peer}", "--force", cwd=HUB)
  else:
    print(f"  hub: {peer} already enrolled at {rows[peer]}")

  # 3. Tunnel.
  keys_path = TUNNEL / "docker/authorized_keys"
  keys = keys_path.read_text(encoding="utf-8")
  ports = {int(p): line for line in keys.splitlines() for p in re.findall(r'permitlisten="0\.0\.0\.0:(\d+)"', line)}
  mine = [p for p, line in ports.items() if line.endswith(f" {peer}-tunnel")]
  port = mine[0] if mine else max([22001, *ports]) + 1
  out = run(*ssh, f"sudo coolify-tunnel-setup {port}")
  line = next(x for x in out.splitlines() if x.startswith("restrict,"))
  if line not in keys:
    if mine:
      keys = keys.replace(ports[port], line)  # a reinstall: the clock's tunnel key is new
    else:
      keys = keys.rstrip("\n") + f"\n\n# {port}: {peer}, a timeclock-system clock (clock {number})\n{line}\n"
    keys_path.write_text(keys, encoding="utf-8")
    git_commit_push(TUNNEL, ["docker/authorized_keys"], f"feat: register {peer} on port {port}")
    deploy_app_named("coolify-tunnel")
  print(f"  tunnel: port {port}")

  # 4. Coolify server, by Coolify's own key (the one the image put in root's authorized_keys).
  coolify_pub = (ROOT / "image/production/files/coolify.pub").read_text(encoding="utf-8").split()[1]
  key_uuid = next(k["uuid"] for k in api("GET", "/security/keys") if coolify_pub in (k.get("public_key") or ""))  # pyright: ignore[reportGeneralTypeIssues]
  servers = [s for s in api("GET", "/servers") if s["name"] == peer]  # pyright: ignore[reportGeneralTypeIssues]
  if servers:
    server_uuid = servers[0]["uuid"]
  else:
    body = {
      "name": peer,
      "description": f"timeclock-system clock {number}",
      "ip": "coolify-tunnel",
      "port": port,
      "user": "root",
      "private_key_uuid": key_uuid,
      "proxy_type": "none",
      "instant_validate": False,
    }
    server_uuid = api("POST", "/servers", body)["uuid"]  # pyright: ignore[reportIndexIssue]
  # The tunnel comes up once coolify-tunnel has redeployed with the new key; validate until Coolify can use the server.
  for _ in range(40):
    api("GET", f"/servers/{server_uuid}/validate")
    time.sleep(30)
    settings = api("GET", f"/servers/{server_uuid}")["settings"]  # pyright: ignore[reportIndexIssue]
    if settings.get("is_reachable") and settings.get("is_usable"):
      break
  else:
    sys.exit(f"Coolify server {peer} isn't reachable and usable after 20 minutes; check it in Coolify, then rerun")
  print(f"  server: {peer} validated")

  # 5. Database login, with a new password (the application doesn't exist yet, so nothing else holds the old one).
  role = f"timeclock_clock_{name.replace('-', '_')}"
  password = secrets.token_urlsafe(32)
  sql = (
    f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
    f"ALTER ROLE {role} LOGIN PASSWORD '{password}'; ELSE "
    f"CREATE ROLE {role} LOGIN PASSWORD '{password}' IN ROLE timeclock_clocks; END IF; END $$;\n"
  )
  run(
    "ssh",
    "-o",
    "BatchMode=yes",
    "Coolify_Server",
    'docker exec -i $(docker ps --format "{{.Names}}" | grep ^timeclock-postgres) psql -U postgres -d timeclock -q -v ON_ERROR_STOP=1',
    stdin=sql,
  )
  print(f"  database: login {role}")

  # 6. The application, beside timeclock-dev's.
  project_uuid = environment_name = None
  for project in api("GET", "/projects"):  # pyright: ignore[reportGeneralTypeIssues]
    for environment in api("GET", f"/projects/{project['uuid']}/environments"):  # pyright: ignore[reportGeneralTypeIssues]
      if environment["id"] == template["environment_id"]:
        project_uuid, environment_name = project["uuid"], environment["name"]
  if project_uuid is None:
    sys.exit("couldn't find timeclock-dev's project and environment")
  body = {
    "project_uuid": project_uuid,
    "environment_name": environment_name,
    "server_uuid": server_uuid,
    "name": peer,
    # Coolify stores the repository as owner/name.
    "git_repository": f"https://github.com/{template['git_repository'].removeprefix('https://github.com/')}",
    "git_branch": template["git_branch"],
    "build_pack": "dockercompose",
    "docker_compose_location": template["docker_compose_location"],
    "base_directory": template["base_directory"],
    "instant_deploy": False,
  }
  app_uuid = api("POST", "/applications/public", body)["uuid"]  # pyright: ignore[reportIndexIssue]
  values = {
    "INSTANCE_ID": name,
    "CLOCK_NUMBER": number,
    "WG_PRIVATE_KEY": wg_private,
    "TIMECLOCK_DB_NAME": "timeclock",
    "TIMECLOCK_DB_USER": role,
    "TIMECLOCK_DB_PASSWORD": password,
  }
  # The rest are the same for every clock (alerts, heartbeat, hub): copied from timeclock-dev's.
  values = {k: e["value"] for k, e in template_envs.items() if k not in values} | values
  api(
    "PATCH", f"/applications/{app_uuid}/envs/bulk", {"data": [{"key": k, "value": v, "is_preview": False} for k, v in values.items()]}
  )
  api("GET", f"/deploy?uuid={app_uuid}")
  print(f"  application: {peer} created and deploying ({', '.join(values)})")
  print("done. The screen shows the clock once the app's health check passes.")


if __name__ == "__main__":
  main()
