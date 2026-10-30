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
2. wireguard-hub, checked only: the clock is enrolled by hand beforehand (its key as timeclock-system-<name>.key in the
   hub checkout, gitignored; a peers.toml row; forwards to the log server and the database relay in rules.v4; a
   release-and-pin), and the pinned release has the row, with the key's public half, and both forwards;
3. coolify-tunnel: the clock's tunnel on the next free port and its line in docker/authorized_keys, pushed and
   redeployed;
4. Coolify: the server (<hostname>-tunnel:<port>, root, no proxy; Coolify allows one server per address whatever the
   port, and an ssh rule in Coolify's container sends every *-tunnel name to coolify-tunnel, see its README), validated,
   and Docker's coolify network on the clock (Coolify creates it only with a proxy; the application joins it);
5. the clock's database login, timeclock_clock_<name> in timeclock_clocks, with a random password;
6. the Coolify application (docker/compose.yaml of this repository, beside timeclock-dev's), its variables filled in:
   the clock's own from the steps above, the shared ones copied from timeclock-dev's application (not PINGKEY or the
   Pushover pair, which the owner sets by hand); named exactly as the
   hostname, through timeclock-dev's GitHub App, watching docker/**, on the predefined network, with Consistent
   Container Names on. Not deployed: the owner sets PINGKEY and the Pushover pair first, if wanted.
The variables stay editable in Coolify; nothing here sets them again once the application exists. It ends by printing
the clock's entry for the workstation's ~/.ssh/config (docs/production-clock-setup.md, "Once: the workstation's SSH
config"), so `ssh <hostname>` goes through the tunnel.

Needs, in .env: COOLIFY_API_TOKEN (a root token). And: ~/.ssh/timeclock_dev (tcadmin's key), `ssh Coolify_Server`,
the wireguard-hub checkout beside this one (read only), and the coolify-tunnel one with push access.
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
HUB_REPO = "AetherBreaker/wireguard-hub"
COOLIFY = "https://coolify.sweetfiretobacco.com/api/v1"
TEMPLATE_INSTANCE = "dev"  # timeclock-dev's application: where the new one goes, and the shared variables' source
OWNER_ONLY = {"PINGKEY", "ALERTS_PUSHOVER_TOKEN", "ALERTS_PUSHOVER_USER_KEY"}  # never copied; the owner sets them
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


def raw_github(repo: str, ref: str, path: str) -> str:
  """A file of a public GitHub repository at a ref."""
  request = urllib.request.Request(
    f"https://raw.githubusercontent.com/{repo}/{ref}/{path}", headers={"User-Agent": "timeclock-new-clock/1"}
  )
  try:
    with urllib.request.urlopen(request, timeout=60) as response:
      return response.read().decode()
  except urllib.error.HTTPError as e:
    sys.exit(f"GitHub {repo}@{ref}:{path}: HTTP {e.code}")


def deploy_app_named(name: str) -> None:
  """Start a deploy of the one Coolify application with this name."""
  apps = [a for a in api("GET", "/applications") if a["name"] == name]  # pyright: ignore[reportGeneralTypeIssues]
  if len(apps) != 1:
    sys.exit(f"expected one Coolify application named {name!r}, found {len(apps)}")
  api("POST", f"/deploy?uuid={apps[0]['uuid']}")
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

  # 2. WireGuard hub: enrolment is by hand (release commands never run from a script), so only check it. The pinned tag is
  # what Coolify deploys; the key file sits in the hub checkout, gitignored.
  run("git", "pull", "--ff-only", cwd=TUNNEL)
  key_path = HUB / f"{peer}.key"
  if not key_path.exists():
    sys.exit(f"{peer} isn't enrolled in wireguard-hub: no {key_path}. Enrol it (wireguard-hub README), release-and-pin, rerun")
  wg_private = key_path.read_text(encoding="utf-8").strip()
  private = X25519PrivateKey.from_private_bytes(base64.b64decode(wg_private))
  public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
  tag = re.search(r"GIT_TAG: (\S+)", raw_github(HUB_REPO, "main", "docker/compose.yaml"))[1]  # pyright: ignore[reportOptionalSubscript]
  peers_text = raw_github(HUB_REPO, tag, "src/wireguard_hub/peers.toml")
  rows = {n: (k, a) for n, k, a in re.findall(r'name = "([^"]+)"\s*\npublic_key = "([^"]+)"\s*\naddress = "([^"]+)"', peers_text)}
  if peer not in rows:
    sys.exit(f"wireguard-hub {tag} (the pinned release) has no {peer} row; enrol it, release-and-pin, rerun")
  if rows[peer][0] != public:
    sys.exit(f"wireguard-hub {tag}'s {peer} row has a different public key than {key_path}; fix one of them by hand")
  rules = raw_github(HUB_REPO, tag, "src/wireguard_hub/rules.v4")
  for target, port in ((LOG_SERVER, 9020), (BACKEND, 5432)):
    rule = (
      f"-A FORWARD -i wg0 -o wg0 -s {rows[peer][1]} -d {rows[target][1]} -p tcp --dport {port} -m conntrack --ctstate NEW -j ACCEPT"
    )
    if rule not in rules:
      sys.exit(f"wireguard-hub {tag}'s rules.v4 lacks {peer}'s forward to {target}:\n{rule}")
  print(f"  hub: {peer} enrolled at {rows[peer][1]} in {tag}")

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
      "ip": f"{peer}-tunnel",
      "port": port,
      "user": "root",
      "private_key_uuid": key_uuid,
      "proxy_type": "none",
      "instant_validate": False,
    }
    server_uuid = api("POST", "/servers", body)["uuid"]  # pyright: ignore[reportIndexIssue]
  # The tunnel comes up once coolify-tunnel has redeployed with the new key; validate until Coolify can use the server.
  for _ in range(40):
    api("POST", f"/servers/{server_uuid}/validate")
    time.sleep(30)
    settings = api("GET", f"/servers/{server_uuid}")["settings"]  # pyright: ignore[reportIndexIssue]
    if settings.get("is_reachable") and settings.get("is_usable"):
      break
  else:
    sys.exit(f"Coolify server {peer} isn't reachable and usable after 20 minutes; check it in Coolify, then rerun")
  print(f"  server: {peer} validated")
  run(*ssh, "sudo docker network inspect coolify >/dev/null 2>&1 || sudo docker network create --attachable coolify")
  print("  network: coolify on the clock")

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
  # Through the GitHub App, like timeclock-dev's, though the repository is public: the App's webhook is what redeploys
  # on a push touching the watch paths.
  github_app = next((g for g in api("GET", "/github-apps") if g["id"] == template["source_id"]), None)  # pyright: ignore[reportGeneralTypeIssues]
  if template["source_type"] != "App\\Models\\GithubApp" or github_app is None:
    sys.exit("timeclock-dev's application isn't deployed through a GitHub App; set its source in Coolify, then rerun")
  body = {
    "project_uuid": project_uuid,
    "github_app_uuid": github_app["uuid"],
    "environment_name": environment_name,
    "server_uuid": server_uuid,
    "name": peer,
    "git_repository": template["git_repository"],
    "git_branch": template["git_branch"],
    "build_pack": "dockercompose",
    "docker_compose_location": template["docker_compose_location"],
    "base_directory": template["base_directory"],
    "instant_deploy": False,
    "watch_paths": "docker/**",
    "connect_to_docker_network": True,
  }
  app_uuid = api("POST", "/applications/private-github-app", body)["uuid"]  # pyright: ignore[reportIndexIssue]
  api("PATCH", f"/applications/{app_uuid}", {"is_consistent_container_name_enabled": True})
  values = {
    "INSTANCE_ID": name,
    "CLOCK_NUMBER": number,
    "WG_PRIVATE_KEY": wg_private,
    "TIMECLOCK_DB_NAME": "timeclock",
    "TIMECLOCK_DB_USER": role,
    "TIMECLOCK_DB_PASSWORD": password,
  }
  # The rest are the same for every clock (alerts, hub): copied from timeclock-dev's. The owner's own channels (PINGKEY,
  # Pushover) are set by hand.
  values = {k: e["value"] for k, e in template_envs.items() if k not in values and k not in OWNER_ONLY} | values
  api(
    "PATCH", f"/applications/{app_uuid}/envs/bulk", {"data": [{"key": k, "value": v, "is_preview": False} for k, v in values.items()]}
  )
  print(f"  application: {peer} created ({', '.join(values)})")
  print(f"done. In Coolify, set {peer}'s {', '.join(sorted(OWNER_ONLY))} if wanted, then deploy it.")
  # The host key is checked under the name this run reached the clock by, which accept-new above recorded.
  print(f"\nAdd to ~/.ssh/config, so `ssh {peer}` goes through the tunnel:\n\nHost {peer}\n  Port {port}\n  HostKeyAlias {host}")


if __name__ == "__main__":
  main()
