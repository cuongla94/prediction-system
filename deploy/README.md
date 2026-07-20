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

The database schema is already applied to the real Supabase instance from
local testing — no need to re-run `db/schema.sql`.

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

No TLS yet — this is plain HTTP, chosen deliberately since nothing served
today is financially sensitive (the paper-trading bot is simulated, no real
money or credentials involved) and no domain was available to get a real
cert against. Revisit with a real domain + Let's Encrypt if that changes.
Credentials aren't stored in this repo; ask whoever set it up (or re-run
`htpasswd` on the droplet to rotate them).

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

```bash
ssh root@<droplet-ip>
cd /opt/kalshi-prediction-market
git pull                          # or re-scp if not using git
sudo -u kalshi uv sync
systemctl restart kalshi-dashboard
```

The crontab doesn't need reinstalling unless `scheduler/crontab.example`
itself changed.
