# Deploying to a droplet

## 1. Create the droplet (DigitalOcean console)

- Image: **Ubuntu 24.04 (LTS) x64**
- Plan: smallest "Basic" tier (1GB RAM is comfortable). This runs a script for
  well under a minute 4x/day plus a dashboard serving one user occasionally —
  there's no reason to pay for more.
- Add your SSH key during creation rather than a password.
- Note the droplet's IP address once it's up.

## 2. Get the code onto the droplet

Either works; git is cleaner for future updates (`git pull` + restart the
service beats re-copying the whole tree each time).

```bash
# Option A: git clone (needs the repo pushed, and either a public repo or a
# deploy key / your SSH key added to GitHub for a private one)
ssh root@<droplet-ip>
git clone git@github.com:cuongla94/prediction-system.git /root/kalshi-prediction-market
cd /root/kalshi-prediction-market

# Option B: copy your local working tree directly, no GitHub auth needed on the server
scp -r . root@<droplet-ip>:/root/kalshi-prediction-market
ssh root@<droplet-ip>
cd /root/kalshi-prediction-market
```

## 3. Run the setup script

```bash
sudo bash deploy/setup.sh
```

Installs system packages, creates a dedicated non-root `kalshi` user, installs
`uv` and the Python dependencies, installs the systemd service (not started
yet) and the crontab, and configures the firewall to allow only SSH.

## 4. Transfer secrets (from your local machine, not through git)

`.env`, `secrets/`, and `certs/` are gitignored on purpose and won't be on the
droplet yet either way — copy them over directly:

```bash
scp .env root@<droplet-ip>:/opt/kalshi-prediction-market/.env
scp -r secrets root@<droplet-ip>:/opt/kalshi-prediction-market/
scp -r certs root@<droplet-ip>:/opt/kalshi-prediction-market/
ssh root@<droplet-ip> "chown -R kalshi:kalshi /opt/kalshi-prediction-market"
```

Apply the current `db/schema.sql` through the database's normal migration
process before deploying this version. In particular, verify `live_orders`,
`live_order_events`, `live_order_fills`, `live_reconciliation_runs`, and
`live_execution_cycles`, plus `bot_control_events.live_enabled`, exist. Do not
enable live automation until that verification passes.

## 5. Start the dashboard and verify

```bash
ssh root@<droplet-ip>
systemctl start kalshi-dashboard
systemctl status kalshi-dashboard    # should show "active (running)"
journalctl -u kalshi-dashboard -n 50 # check for startup errors
```

The dashboard itself still binds to `127.0.0.1` only (see `dashboard/app.py` and
`deploy/kalshi-dashboard.service`) — gunicorn is never directly reachable from
the internet. As of 2026-07-19, `nginx` sits in front as a reverse proxy on
port 80, with HTTP Basic Auth (`/etc/nginx/.htpasswd`, config at
`/etc/nginx/sites-available/kalshi-dashboard`) — that's now the supported way
to reach it directly:

```
http://<droplet-ip>/
```

Because the dashboard now exposes production automation controls, plain
internet-facing HTTP is not an acceptable deployment posture. Use the SSH
tunnel below until a real domain and TLS termination are installed.
Credentials remain outside this repository.

The SSH tunnel still works too, and bypasses Basic Auth entirely (talks to
gunicorn directly, not through nginx) — useful for local debugging:

```bash
ssh -L 8000:localhost:8000 root@<droplet-ip>
# then open http://localhost:8000 in your local browser
```

## 6. Confirm the cron schedule is installed

```bash
ssh root@<droplet-ip> "sudo -u kalshi crontab -l"
```

Should show the entry from `scheduler/crontab.example` with the path already
substituted to `/opt/kalshi-prediction-market`. Logs land in
`/opt/kalshi-prediction-market/logs/pipeline.log`.

## Updating after a code change

**Push to `main`.** That's the whole procedure as of 2026-07-20 —
`.github/workflows/deploy.yml` runs the test suite and ruff, and only if both
pass does it SSH to the droplet, move it to that exact commit, `uv sync`,
reinstall the crontab if `scheduler/crontab.example` changed, restart both
services, and verify they came back healthy.

> The instructions that used to live here (`git pull` + `sudo -u kalshi uv sync`
> + restart) were wrong on two counts, which is worth recording rather than
> quietly deleting: `/opt/kalshi-prediction-market` was an `rsync` copy with no
> `.git` at all, so `git pull` could never have worked; and `sudo -u kalshi uv
> sync` fails because that account has no `~/.local/bin` on PATH — the same
> gotcha `scheduler/run_pipeline.sh` already documents. They also only ever
> restarted `kalshi-dashboard`, silently leaving `kalshi-price-feed` on old code.

### How the deploy is locked down

The CI key authenticates as root but is **not** a general-purpose root key. In
`/root/.ssh/authorized_keys` it is pinned to a forced command:

```
restrict,command="/usr/local/bin/kalshi-deploy" ssh-ed25519 AAAA... github-actions-deploy
```

`command=` makes sshd ignore whatever the client asks to run and execute only
that script; `restrict` disables pty allocation, port/agent/X11 forwarding and
user rc files. The requested command reaches the script as
`$SSH_ORIGINAL_COMMAND`, which it treats as untrusted input and accepts only as
a 40-char hex SHA that is genuinely an ancestor of `origin/main` and not older
than what's deployed. So a leaked `DEPLOY_SSH_KEY` can trigger a redeploy of
already-published code and nothing else — no shell, no reading `.env`.

`/usr/local/bin/kalshi-deploy` lives on the droplet only (root:root 0755) and
is deliberately tiny and stable. The real logic is `deploy/remote_deploy.sh` in
this repo, which it `exec`s from the freshly-checked-out tree — so deploy
changes get code-reviewed like anything else.

Three GitHub Actions secrets are required: `DEPLOY_SSH_KEY` (the CI private
key), `DEPLOY_KNOWN_HOSTS` (`ssh-keyscan -t ed25519 <droplet-ip>`), and
`DEPLOY_HOST` (`root@<droplet-ip>`, a secret so the address isn't published in
a public repo).

Note `.git` is root-owned `0700` while the rest of the tree belongs to
`kalshi`. That's load-bearing: a `kalshi`-writable `.git` would let that
account set `filter.*.smudge` in `.git/config`, which root's own git commands
then execute. It's also why every `uv run` in `scheduler/*.sh` passes
`--no-sync` — `uv run` otherwise tries to re-lock and write to the project root.

### Rolling back, or deploying by hand

Your own operator key still has a normal shell. The deploy wrapper refuses to
move to an older commit on purpose (an automated rollback should never happen
silently), so a rollback is a deliberate manual act:

```bash
ssh root@<droplet-ip>
cd /opt/kalshi-prediction-market
git fetch origin main
git reset --hard <known-good-sha>
bash deploy/remote_deploy.sh <known-good-sha>
```

`git reset --hard` does not remove untracked files, so `.env`, `secrets/`,
`certs/`, `logs/*.log` and `.venv/` survive it.
