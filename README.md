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

The system has four parts:

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
/srv/veganactivistsnl-bot/.venv
```

This keeps Python dependencies separate from system packages.

### 3. signal-cli daemon

`signal-cli` runs as a persistent daemon and exposes a local JSON-RPC Unix socket at:

```
/srv/veganactivistsnl-bot/run/signal-cli.sock
```

The Python bot connects to that socket instead of spawning a fresh `signal-cli` process for every command.

### 4. systemd services

Three systemd units manage the bot:

| Unit               | Purpose                             |
| ------------------ | ----------------------------------- |
| `signal-cli-daemon.service` | Runs persistent `signal-cli` daemon |
| `bot.service`    | Runs the bot                        |
| `bot-poll.timer` | Checks for git updates every minute |

When a new commit is detected:

1. the repository is reset to `origin/main`
2. the service is restarted
3. dependencies are installed if needed

---

# Server Setup

These steps are only needed once.

## 1. Install Nix and Git

Install the multi-user version of Nix:

```
sh <(curl -L https://nixos.org/nix/install) --daemon
```

Enable flakes:

```
sudo mkdir -p /etc/nix
echo "experimental-features = nix-command flakes" | sudo tee /etc/nix/nix.conf
```

Install Git:

```
nix profile install nixpkgs#git
```

---
## 2. Create project directory

```
mkdir /srv/
sudo chown $(whoami) /srv
cd /srv
git clone https://github.com/veganfuture/veganactivistsnl-bot.git
```

---

## 3. First time link Signal device

```
cd /srv/veganactivistsnl-bot
nix develop
```

Before the bot will work you need to link the new device (the machine you're on) to the Signal bot, see "Link the bot to Signal".

After this try to run the bot manually first, see "Run the bot locally".

## 4. Install services

Before installing the services, create an env file that systemd will load for the bot:

```
cd /srv/veganactivistsnl-bot
cat > .env <<'EOF'
SIGNAL_ACCOUNT=+123456789
EOF
```

`bot.service` and `signal-cli-daemon.service` read `/srv/veganactivistsnl-bot/.env`, so make sure `SIGNAL_ACCOUNT` is set correctly before you start the services.

Then run the flake installer:

```
nix run .#install
```

The installer writes the systemd units to run as the current shell user.

This installs and enables:

* `signal-cli-daemon.service`
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

This uses the persistent `signal-cli` daemon by default, so for local testing the most reliable flow is:

1. Create a local `.env` file:

```bash
cd /path/to/veganactivistsnl-bot
cat > .env <<'EOF'
SIGNAL_ACCOUNT=+31612345678
EOF
```

2. Start the signal-cli daemon in one terminal:

```bash
nix run .#install
systemctl status signal-cli-daemon.service
journalctl -u signal-cli-daemon.service -f
```

3. Start the bot in another terminal:

```bash
nix develop
python -m bot --verbose
```

If you want to run everything locally without installing any systemd services, use two terminals:

Terminal 1, start the daemon:

```bash
nix develop
set -a
source .env
set +a
mkdir -p tmp run
TMPDIR="$PWD/tmp" signal-cli -u "$SIGNAL_ACCOUNT" daemon --socket "$PWD/run/signal-cli.sock" --receive-mode on-connection
```

Terminal 2, start the bot:

```bash
nix develop
set -a
source .env
set +a
python -m bot --verbose --signal-client-mode daemon --signal-daemon-socket-path "$PWD/run/signal-cli.sock"
```

Or first run 

```
nix develop
```

and then

```
SIGNAL_ACCOUNT=+31612345678 python -m bot 
```

Of course the account phone number needs to match the bot's phone number.

If you want to compare with the old subprocess mode locally, you can force CLI mode:

```bash
SIGNAL_ACCOUNT=+31612345678 python -m bot --verbose --signal-client-mode cli
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

View the signal-cli daemon logs:

```
journalctl -u signal-cli-daemon.service -f
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
/srv/veganactivistsnl-bot/.env
```

Example content:

```
SIGNAL_ACCOUNT=+123456789
BOT_STATE_FILE=/srv/veganactivistsnl-bot/data/group_state.json
SIGNAL_CLIENT_MODE=daemon
```

To enable this, ensure the systemd unit contains:

```
EnvironmentFile=/srv/veganactivistsnl-bot/.env
```

After editing:

```
sudo systemctl daemon-reload
sudo systemctl restart signal-cli-daemon.service
sudo systemctl restart bot.service
```

## CLI options

You can also pass configuration via CLI flags (these override environment variables):

```
python -m bot --account +123456789 --state-path /srv/veganactivistsnl-bot/data/group_state.json
```

---

# Link the bot to Signal

The bot should run as a **linked device** on an existing Signal account. Do this once on the server, as the same user that runs the service (default `ubuntu`).

1. Generate a QR code on the server:

```
signal-cli link -n "veganactivistsnl-bot"
```

2. On your phone: Signal → Settings → Linked devices → **Link new device**, then scan the QR code.

3. Confirm the link worked:

```
signal-cli listDevices
```

Signal state is stored under `~/.local/share/signal-cli`, so the same user must run the bot and the linking step.

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
