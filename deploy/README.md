# Deploying MTGC as a Production Web Service

This guide covers deploying the MTG Card Collection Builder as a persistent web service on an Ubuntu machine, with automated deployment via GitHub Actions.

## Architecture

```
GitHub repo
    │
    │  push to main triggers deploy workflow
    ▼
Self-hosted runner
    │
    │  deploy.sh
    ▼
┌──────────────────────────────────────┐
│  Ubuntu server                       │
│                                      │
│  nginx (:8082) ──► mtgc (:8081)     │
│                                      │
│  User: mtgc (dedicated system user)  │
│  Code: /opt/mtgc                     │
│  Data: /var/lib/mtgc                 │
└──────────────────────────────────────┘
```

## Quick Start

```bash
# 1. Install prerequisites
sudo apt install nginx curl git

# 2. Clone the setup script (or copy it from an existing checkout)
git clone https://github.com/thaen/efj-mtgc.git /tmp/efj-mtgc
sudo bash /tmp/efj-mtgc/deploy/setup.sh
```

`setup.sh` handles everything: creates a dedicated `mtgc` system user, clones the repo to `/opt/mtgc`, installs a private copy of uv, downloads card data (~700MB), installs the systemd service, and configures nginx. Everything is self-contained under `/opt/mtgc` and `/var/lib/mtgc`.

## Prerequisites

- Ubuntu server (tested on 22.04/24.04)
- nginx (`sudo apt install nginx`)
- Python 3.12+
- git, curl

## Step-by-Step Setup

If you prefer to run each step manually, or need to customize the defaults.

### 1. Create the service user

```bash
sudo useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/mtgc mtgc
```

### 3. Create the data directory

Must exist before installing uv or running any commands as `mtgc`, since it's the user's home directory and uv caches here.

```bash
sudo mkdir -p /var/lib/mtgc
sudo chown mtgc:mtgc /var/lib/mtgc
```

### 4. Clone the repository and install uv

```bash
sudo git clone https://github.com/thaen/efj-mtgc.git /opt/mtgc
sudo chown -R mtgc:mtgc /opt/mtgc

# Install uv privately for the mtgc user
sudo mkdir -p /opt/mtgc/.uv
sudo chown mtgc:mtgc /opt/mtgc/.uv
curl -LsSf https://astral.sh/uv/install.sh | sudo -u mtgc env UV_INSTALL_DIR=/opt/mtgc/.uv/bin sh

cd /opt/mtgc
sudo -u mtgc /opt/mtgc/.uv/bin/uv sync
```

### 5. Download card data

~700MB total: Scryfall bulk data + MTGJSON price data.

```bash
sudo -u mtgc MTGC_HOME=/var/lib/mtgc /opt/mtgc/.uv/bin/uv run mtg setup
sudo -u mtgc MTGC_HOME=/var/lib/mtgc /opt/mtgc/.uv/bin/uv run mtg data fetch-prices
```

### 6. Install the systemd service

```bash
sudo cp /opt/mtgc/deploy/mtgc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mtgc
```

Verify:

```bash
sudo systemctl status mtgc
curl http://localhost:8081/
```

### 7. Install the nginx config

The default config listens on port 8082. Edit `deploy/mtgc-nginx.conf` to change the port.

```bash
sudo cp /opt/mtgc/deploy/mtgc-nginx.conf /etc/nginx/sites-available/mtgc
sudo ln -sf /etc/nginx/sites-available/mtgc /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Verify:

```bash
curl http://localhost:8082/
curl http://localhost:8082/api/cached-sets
```

### 8. Set up automated deployment (optional)

To auto-deploy on every push to main, register a GitHub Actions self-hosted runner on your repo (or fork).

Go to GitHub: **Settings > Actions > Runners > New self-hosted runner**. Follow the instructions, then install as a systemd service:

```bash
cd ~/actions-runner
sudo ./svc.sh install
sudo ./svc.sh start
```

The deploy workflow (`.github/workflows/deploy.yml`) triggers on push to main, pulls the latest code as the `mtgc` user, and runs `deploy/deploy.sh`. The deploy script needs sudo access — configure sudoers for your runner user:

```
runner-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart mtgc
runner-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl reload nginx
runner-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl daemon-reload
runner-user ALL=(ALL) NOPASSWD: /usr/bin/cp /opt/mtgc/deploy/mtgc.service /etc/systemd/system/mtgc.service
runner-user ALL=(ALL) NOPASSWD: /usr/bin/cp /opt/mtgc/deploy/mtgc-nginx.conf /etc/nginx/sites-available/mtgc
runner-user ALL=(ALL) NOPASSWD: /usr/bin/ln -sf /etc/nginx/sites-available/mtgc /etc/nginx/sites-enabled/
runner-user ALL=(ALL) NOPASSWD: /usr/sbin/nginx -t
runner-user ALL=(ALL) NOPASSWD: /usr/bin/mkdir -p /var/lib/mtgc
runner-user ALL=(ALL) NOPASSWD: /usr/bin/chown mtgc\:mtgc /var/lib/mtgc
runner-user ALL=(mtgc) NOPASSWD: ALL
```

## How It Works

1. Push to main triggers the deploy workflow
2. The self-hosted runner pulls the latest code into `/opt/mtgc`
3. `deploy/deploy.sh` syncs dependencies, copies changed configs, restarts the service, and runs a health check

## What's in `deploy/`

| File | Purpose |
|---|---|
| `setup.sh` | One-time machine provisioning (run with sudo) |
| `deploy.sh` | Called by CI on every push (handles incremental updates) |
| `mtgc.service` | systemd unit file for the Python server |
| `mtgc-nginx.conf` | nginx reverse proxy config |

## Port Summary

| Port | Used by | Purpose |
|---|---|---|
| 8081 | mtgc (Python) | Application server (internal) |
| 8082 | nginx | Public-facing reverse proxy |

Both ports are configurable. Change 8081 in `mtgc.service` and `mtgc-nginx.conf`. Change 8082 in `mtgc-nginx.conf`.

## Troubleshooting

**Service won't start:**

```bash
sudo journalctl -u mtgc -n 50 --no-pager
```

**Service crashes with "AllPricesToday.json not found":**

```bash
sudo -u mtgc MTGC_HOME=/var/lib/mtgc /opt/mtgc/.uv/bin/uv run mtg data fetch-prices
sudo systemctl restart mtgc
```

**Deploy workflow fails:**

Check the Actions tab. Common issues:
- Runner offline — `sudo systemctl status actions.runner.*`
- Sudoers not configured — deploy.sh can't restart services
- Permissions — `/opt/mtgc` must be owned by `mtgc:mtgc`

**nginx returns 502:**

The Python service isn't running or hasn't finished starting. Check `systemctl status mtgc` and wait a few seconds for startup.

**SSE streaming (ingest pages) not working through nginx:**

Verify `proxy_buffering off` is in the nginx config. Some environments also need `proxy_cache off` — add it to the `location /` block if SSE events are being batched.
