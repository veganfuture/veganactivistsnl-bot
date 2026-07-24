# Signal Bot (Nix + systemd + Git polling)

This project runs a **Signal bot** on a small Linux server using a very simple deployment model:

* **Push to `main`**
* The server **polls the repository**
* If there is a new commit:

  * it **pulls the repo**
  * **restarts the bot**

Dependencies are automatically installed by Nix and uv.

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


## 0. Requirements

Requires a Linux machine with bash and systemd installed. Any regular flavour distribution should work: Fedora, Ubuntu, Arch, Redhat. Ironically NixOS won't work, because you can not just intsall systemd services on NixOs (the flake would have to expose nixosModules that can be imported in a NixOs config, which it currently doesn't).

## 1. Install Nix and Git

Install the multi-user version of Nix:

```sh
sh <(curl -L https://nixos.org/nix/install) --daemon
```

Enable flakes:

```sh
sudo mkdir -p /etc/nix
echo "experimental-features = nix-command flakes" | sudo tee /etc/nix/nix.conf
```

Install Git:

```sh
nix profile install nixpkgs#git
```

---
## 2. Create project directory

```sh
mkdir /srv/
sudo chown $(whoami) /srv
cd /srv
git clone https://github.com/veganfuture/veganactivistsnl-bot.git
```

---

## 3. First time link Signal device

Before the bot will work you need to link the new device (the machine you're on) to the Signal bot, see "Link the bot to Signal".

## 4. Install services

To setup systemd services that run the bot and keep the bot up to date, run:

```sh
nix run .#install -- --signal-account '+316...' --config configs/prod.toml
```

This installs and enables the following systemd services:

* `signal-daemon.service`
* `bot.service`
* `bot-poll.timer`

The bot should start automatically.

To see if the bot is running and connected to Signal check:

```sh
journalctl -u bot.service -f
```

If you don't see any recent warnings or errors and you see a message: "_Connected to signal-cli daemon socket_" or see it sending and receiving messages, then you can assume it is connected and running.

To remove the services later:

```sh
nix run .#uninstall
```

---

# Deployment Workflow

Deployment is extremely simple.

```sh
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

```sh
install-precommit-hooks
```

You should now have precommit hookt hat runs type checks, linters, formatters and unit tests before you are allowed to commit.

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

```sh
nix run .#poll-once
```

This performs one polling cycle.

---

# Logs

View the bot logs:

```sh
journalctl -u bot.service -f
```

View the signal daemon logs:

```sh
journalctl -u signal-daemon.service -f
```

View deployment checks:

```sh
journalctl -u bot-poll.service -f
```

---

# systemd Timers

List timers:

```sh
systemctl list-timers
```

You should see:

```sh
bot-poll.timer
```

This runs once per minute.

---

# Link the bot to Signal

The bot should run as a **linked device** on an existing Signal account. Do this once on the server, as the same user that runs the service (default `ubuntu`).

1. Generate a QR code on the server for the machine your're own:

```
nix run .#link -- -machine-name <MY-MACHINE-NAME>
```

If you don't see a QR code, because your terminal does not support graphic display, the take the `sgnl://` address and generate a QR Code to it. 

2. On your phone: Signal → Settings → Linked devices → **Link new device**, then scan the QR code.

3. The bot should now show up under linked devices in Signal. Signal state is stored under `~/.local/share/signal-cli`, so the same user must run the bot and the linking step.

---

# Troubleshooting

## Bot not starting

Check service status:

```
systemctl status bot.service
systemctl status signal-daemon.service
```

---

## Check logs

```
journalctl -u bot.service -n 100
journalctl -u signal-daemon.service -n 100
```

---

## Force redeploy

```
nix run .#poll-once
```
