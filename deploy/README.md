# MTGC Deployment

Container-based deployment using Podman Quadlet with CI via GitHub Actions.

## Architecture

```
GitHub repo
    │
    │  push to main triggers deploy workflow
    ▼
Self-hosted runner
    │
    │  deploy.sh (podman build + systemctl restart)
    ▼
┌──────────────────────────────────────┐
│  Server                              │
│                                      │
│  Podman container (:8081)            │
│  Data: mtgc-data volume              │
│  Env:  ~/.config/mtgc/.env           │
└──────────────────────────────────────┘
```

## Prerequisites

- Podman 4.4+
- Anthropic API key
- curl (for health checks)

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/thaen/efj-mtgc.git /opt/mtgc
```

### 2. Configure environment

```bash
mkdir -p ~/.config/mtgc
cp /opt/mtgc/.env.example ~/.config/mtgc/.env
# Edit ~/.config/mtgc/.env and set ANTHROPIC_API_KEY
```

### 3. Build the container image

```bash
cd /opt/mtgc
podman build -t mtgc -f Containerfile .
```

### 4. Install the Quadlet

The `.container` file lives in the deploy repo (`efj-mtgc-deploy`). Copy it into place:

```bash
mkdir -p ~/.config/containers/systemd
cp deploy/mtgc.container ~/.config/containers/systemd/
systemctl --user daemon-reload
```

### 5. Start the service

```bash
systemctl --user start mtgc
```

### 6. First-run data initialization

```bash
podman exec -it mtgc mtg setup
```

### 7. Enable on boot (optional)

```bash
systemctl --user enable mtgc
loginctl enable-linger $USER
```

## Automated Deployment (CI)

Register a GitHub Actions self-hosted runner, then pushes to main will automatically rebuild the image and restart the container.

No sudo or special permissions required — everything runs as the runner user via `podman` and `systemctl --user`.

## What's in `deploy/`

| File | Purpose |
|---|---|
| `deploy.sh` | Called by CI — builds image, restarts service, health check |

The Quadlet `.container` file and sudoers config live in [`efj-mtgc-deploy`](https://github.com/rgantt/efj-mtgc-deploy).

## Troubleshooting

```bash
# Check container status
systemctl --user status mtgc
podman ps -a

# View logs
journalctl --user -u mtgc -f

# Shell into the container
podman exec -it mtgc bash

# Inspect the data volume
podman volume inspect mtgc-data
```
