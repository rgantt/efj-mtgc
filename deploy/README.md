# MTGC Deployment

Rootless Podman Quadlet deployment. No sudo required (after initial podman install). Each instance is a separate repo clone with its own image, data, and config.

## Prerequisites (one-time, needs sudo)

```bash
sudo apt install podman
loginctl enable-linger $USER
```

## One-time: set up default env

Create a shared env file so new instances automatically get your API key:

```bash
mkdir -p ~/.config/mtgc
echo "ANTHROPIC_API_KEY=sk-ant-..." > ~/.config/mtgc/default.env
chmod 600 ~/.config/mtgc/default.env
```

## Stable deployment (CD)

Push to main auto-deploys the `prod` instance.

```bash
git clone https://github.com/thaen/efj-mtgc.git /opt/mtgc-prod
cd /opt/mtgc-prod
bash deploy/setup.sh prod 8081
systemctl --user start mtgc-prod
podman exec -it systemd-mtgc-prod mtg setup
```

## Feature / test instances

Each instance runs from its own checkout on any branch. Port is auto-assigned if omitted.

```bash
git clone https://github.com/thaen/efj-mtgc.git ~/workspace/mtgc-feature-xyz
cd ~/workspace/mtgc-feature-xyz
git checkout feature-xyz
bash deploy/setup.sh feature-xyz
systemctl --user start mtgc-feature-xyz
podman exec -it systemd-mtgc-feature-xyz mtg setup

# ... develop and test ...

# Clean up when done
bash deploy/teardown.sh feature-xyz         # keeps data volume
bash deploy/teardown.sh feature-xyz --purge  # removes everything
```

## Scripts

| Script | Purpose |
|---|---|
| `setup.sh <name> [port]` | Create instance. Port auto-assigned if omitted. Copies API key from `~/.config/mtgc/default.env` |
| `deploy.sh <name>` | Rebuild image and restart one instance |
| `teardown.sh <name> [--purge]` | Stop and remove instance. `--purge` deletes data volume and env file |

## CI

Push to main auto-deploys `prod`. Use workflow_dispatch to deploy other instances by name.

## Troubleshooting

```bash
systemctl --user status mtgc-<name>
journalctl --user -u mtgc-<name> -f
podman exec -it systemd-mtgc-<name> bash
podman volume inspect systemd-mtgc-<name>-data
```
