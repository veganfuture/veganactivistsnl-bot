# Signal Bot (Nix + systemd + Git polling)

This project runs a **Signal bot** on a small Linux server (e.g. an AWS Lightsail instance) using a very simple deployment model:

* **Push to `main`**
* The server **polls the repository**
* If there is a new commit:

  * it **pulls the repo**
  * **restarts the bot**
  * Python dependencies are installed automatically

The runtime environment is provided by **Nix**, while Python dependencies are installed using **uv** into a local virtual environment.

This avoids containers, CI/CD pipelines, and complex deployment tools while still giving reproducible system dependencies.

---

# Architecture

The system has four parts:

### 1. Nix runtime

Nix provides the system-level tools:

* Python
* uv
* signal-cli
* git

This ensures the server always runs with the correct versions.

### 2. Python virtual environment

Python dependencies are installed via:

```sh
uv sync --frozen
```

### 3. signal-cli daemon

`signal-cli` runs as a persistent daemon and exposes a local JSON-RPC Unix socket at:

```
$repo_dir/run/signal-cli.sock
```

The Python bot connects to that socket instead of spawning a fresh `signal-cli` process for every command.

### 4. systemd services

Three systemd units manage the bot:

| Unit               | Purpose                             |
| ------------------ | ----------------------------------- |
| `signal-daemon.service` | Runs persistent `signal-cli` daemon |
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

Run the flake installer with the Signal account and repository path:

```
nix run .#install -- --runtime --signal-account '+316...' --repo-dir . --verbose-level 1
```

The installer writes the systemd units to run as the current shell user. Pass `--runtime` to install runtime units in `/run/systemd/system`.

This installs and enables:

* `signal-daemon.service`
* `bot.service`
* `bot-poll.timer`

The bot should start automatically.

To remove the services later:

```bash
nix run .#uninstall
```

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

## Install precommit hooks

Once you're in a nix development shell run:

```
install-precommit-hooks
```

You should now have precommit hooks that run type checks, linters, formatters and unit tests.

### Run the bot locally

Before the bot's signal daemon will work you need to link the new device (the machine you're on) to the Signal bot, see "Link the bot to Signal".

Run the signal daemon:

```sh
nix run .#signal-daemon -- --signal-acount +316... 
```

You can also make a $SIGNAL_ACCOUNT environment variable (put that in your .envrc), so that you never need to supply the phone number.

Then run the bot from a nix dev shell:

```sh
bot --config configs/test.toml
```

### Test the update process manually

```
nix run .#poll-once
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
journalctl -u signal-daemon.service -f
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

## CLI options

Configuration is passed through CLI flags and installer arguments. For service installation, use `nix run .#install -- --runtime --signal-account ... --repo-dir ...` and add `--verbose-level 1` or `--verbose-level 2` if you want daemon verbosity.

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
nix run .#poll-once
```
