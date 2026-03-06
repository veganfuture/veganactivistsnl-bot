# Signal Bot (Nix + systemd + Git polling)

This project runs a **Signal bot** on a small Linux server (e.g. an AWS Lightsail instance) using a very simple deployment model:

* **Push to `main`**
* The server **polls the repository**
* If there is a new commit:

  * it **pulls the repo**
  * **restarts the bot**
  * Python dependencies are installed automatically

The runtime environment is provided by **Nix**, while Python dependencies are installed using **pip** into a local virtual environment.

This avoids containers, CI/CD pipelines, and complex deployment tools while still giving reproducible system dependencies.

---

# Architecture

The system has three parts:

### 1. Nix runtime

Nix provides the system-level tools:

* Python
* pip / venv
* signal-cli
* git

This ensures the server always runs with the correct versions.

### 2. Python virtual environment

Python dependencies are installed via:

```
requirements.txt
```

They are installed into:

```
/srv/bot/.venv
```

This keeps Python dependencies separate from system packages.

### 3. systemd services

Two systemd units manage the bot:

| Unit               | Purpose                             |
| ------------------ | ----------------------------------- |
| `bot.service`    | Runs the bot                        |
| `bot-poll.timer` | Checks for git updates every minute |

When a new commit is detected:

1. the repository is reset to `origin/main`
2. the service is restarted
3. dependencies are installed if needed

---

# Repository Structure

Example structure:

```
.
├─ flake.nix
├─ flake.lock
├─ requirements.txt
└─ bot/
   └─ main.py
```

---

# Server Setup

These steps are only needed once.

## 1. Create project directory

```
sudo mkdir -p /srv/bot
sudo chown -R ubuntu:ubuntu /srv/bot
```

## 2. Clone the repository

```
git clone <your-repo-url> /srv/bot
```

---

## 3. Install Nix

Install the multi-user version of Nix:

```
sh <(curl -L https://nixos.org/nix/install) --daemon
```

Enable flakes:

```
sudo mkdir -p /etc/nix
echo "experimental-features = nix-command flakes" | sudo tee /etc/nix/nix.conf
```

---

## 4. Install services

Run the flake installer:

```
cd /srv/bot
nix run .#install
```

This installs and enables:

* `bot.service`
* `bot-poll.timer`

The bot should start automatically.

---

# Deployment Workflow

Deployment is extremely simple.

```
git push origin main
```

Within about **60 seconds**, the server will:

1. detect the new commit
2. pull the repository
3. restart the bot

No manual deployment is required.

---

# Development

### Run the bot locally

```
nix run .#run
```

### Configure state path

By default the bot stores its JSON state at `data/group_state.json`. Override it with:

```
python -m bot.main --state-path /srv/bot/data/group_state.json
```

---

### Test the update process manually

```
nix run .#pollOnce
```

This performs one polling cycle.

---

# Logs

View the bot logs:

```
journalctl -u bot.service -f
```

View deployment checks:

```
journalctl -u bot-poll.service -f
```

---

# systemd Timers

List timers:

```
systemctl list-timers
```

You should see:

```
bot-poll.timer
```

This runs once per minute.

---

# Configuration

## Environment variables

You can configure the bot using a `.env` file.

Example:

```
/srv/bot/.env
```

Example content:

```
SIGNAL_ACCOUNT=+123456789
```

To enable this, ensure the systemd unit contains:

```
EnvironmentFile=/srv/bot/.env
```

After editing:

```
sudo systemctl daemon-reload
sudo systemctl restart bot.service
```

---

# Updating Python dependencies

Edit:

```
requirements.txt
```

Then push the change:

```
git commit -am "update dependencies"
git push
```

The next poll cycle will restart the service and reinstall dependencies if necessary.

---

# Troubleshooting

## Bot not starting

Check service status:

```
systemctl status bot.service
```

---

## Check logs

```
journalctl -u bot.service -n 100
```

---

## Force redeploy

```
nix run .#pollOnce
```
