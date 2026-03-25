{
  description = "Signal bot (Nix provides python + signal-cli, pip provides deps)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = {
    self,
    nixpkgs,
    flake-utils,
  }:
    flake-utils.lib.eachDefaultSystem (system: let
      pkgs = import nixpkgs {inherit system;};

      repoDir = "/srv/veganactivistsnl-bot";
      venvDir = "${repoDir}/.venv";
      tmpDir = "${repoDir}/tmp";
      runDir = "${repoDir}/run";
      signalSocketPath = "${runDir}/signal-cli.sock";

      runtimePkgs = [
        pkgs.bash
        pkgs.coreutils
        pkgs.git
        pkgs.python3
        pkgs.signal-cli
      ];

      runBot = pkgs.writeShellApplication {
        name = "bot-run";
        runtimeInputs = runtimePkgs;
        text = ''
          set -euo pipefail
          cd "${repoDir}"

          mkdir -p "${tmpDir}"
          export TMPDIR="${tmpDir}"
          export JAVA_TOOL_OPTIONS="-Djava.io.tmpdir=${tmpDir}"

          if [ ! -d "${venvDir}" ]; then
            python -m venv "${venvDir}"
          fi

          # Activate venv
          # shellcheck disable=SC1091
          source "${venvDir}/bin/activate"

          # Install deps (fast when already satisfied)
          if [ -f requirements.txt ]; then
            python -m pip install -r requirements.txt
          fi

          exec python -m bot \
            --signal-client-mode daemon \
            --signal-daemon-socket-path "${signalSocketPath}"
        '';
      };

      runSignalDaemon = pkgs.writeShellApplication {
        name = "signal-daemon-run";
        runtimeInputs = runtimePkgs;
        text = ''
          set -euo pipefail
          cd "${repoDir}"

          if [ -z "''${SIGNAL_ACCOUNT:-}" ]; then
            echo "SIGNAL_ACCOUNT must be set in ${repoDir}/.env" >&2
            exit 1
          fi

          mkdir -p "${tmpDir}" "${runDir}"
          export TMPDIR="${tmpDir}"
          export JAVA_TOOL_OPTIONS="-Djava.io.tmpdir=${tmpDir}"
          rm -f "${signalSocketPath}"

          exec signal-cli -u "$SIGNAL_ACCOUNT" daemon --socket "${signalSocketPath}" --receive-mode on-connection
        '';
      };

      pollOnce = pkgs.writeShellApplication {
        name = "bot-poll-once";
        runtimeInputs = runtimePkgs ++ [pkgs.systemd pkgs.nix];
        text = ''
          set -euo pipefail
          cd "${repoDir}"

          REMOTE="origin"
          BRANCH="main"

          git fetch "$REMOTE" "$BRANCH"

          localRev="$(git rev-parse HEAD)"
          remoteRev="$(git rev-parse "$REMOTE/$BRANCH")"

          if [ "$localRev" = "$remoteRev" ]; then
            echo "No changes ($localRev)."
            exit 0
          fi

          echo "Updating: $localRev -> $remoteRev"
          git reset --hard "$REMOTE/$BRANCH"

          # Restart the daemon and bot so both pick up code/config changes.
          systemctl restart signal-cli-daemon.service
          systemctl restart bot.service
          echo "Restarted signal-cli-daemon.service and bot.service"
        '';
      };

      serviceUnit = pkgs.writeText "bot.service" ''
        [Unit]
        Description=Signal bot (Nix runtime + pip venv)
        Requires=signal-cli-daemon.service
        After=signal-cli-daemon.service
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        User=ubuntu
        WorkingDirectory=${repoDir}

        # Your bot gets signal-cli + python from Nix, deps from venv
        ExecStart=${runBot}/bin/bot-run

        Restart=always
        RestartSec=2
        StandardOutput=journal
        StandardError=journal

        Environment=SIGNAL_CLIENT_MODE=daemon
        Environment=SIGNAL_DAEMON_SOCKET_PATH=${signalSocketPath}
        Environment=TMPDIR=${tmpDir}
        Environment=JAVA_TOOL_OPTIONS=-Djava.io.tmpdir=${tmpDir}
        EnvironmentFile=${repoDir}/.env

        [Install]
        WantedBy=multi-user.target
      '';

      daemonServiceUnit = pkgs.writeText "signal-cli-daemon.service" ''
        [Unit]
        Description=signal-cli daemon (JSON-RPC over Unix socket)
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        User=ubuntu
        WorkingDirectory=${repoDir}
        ExecStart=${runSignalDaemon}/bin/signal-daemon-run

        Restart=always
        RestartSec=2
        StandardOutput=journal
        StandardError=journal

        Environment=TMPDIR=${tmpDir}
        Environment=JAVA_TOOL_OPTIONS=-Djava.io.tmpdir=${tmpDir}
        EnvironmentFile=${repoDir}/.env

        [Install]
        WantedBy=multi-user.target
      '';

      pollServiceUnit = pkgs.writeText "bot-poll.service" ''
        [Unit]
        Description=Poll git main and restart Signal bot

        [Service]
        Type=oneshot
        User=ubuntu
        WorkingDirectory=${repoDir}
        ExecStart=${pollOnce}/bin/bot-poll-once
      '';

      pollTimerUnit = pkgs.writeText "bot-poll.timer" ''
        [Unit]
        Description=Poll git main every minute for Signal bot

        [Timer]
        OnBootSec=30
        OnUnitActiveSec=60
        Persistent=true

        [Install]
        WantedBy=timers.target
      '';

      install = pkgs.writeShellApplication {
        name = "bot-install";
        runtimeInputs = [pkgs.bash pkgs.coreutils pkgs.gnused pkgs.systemd];
        text = ''
          set -euo pipefail

          bot_user="''${SUDO_USER:-$USER}"
          temp_dir="$(mktemp -d)"
          trap 'rm -rf "$temp_dir"' EXIT

          sed "s/^User=.*/User=$bot_user/" ${serviceUnit} > "$temp_dir/bot.service"
          sed "s/^User=.*/User=$bot_user/" ${daemonServiceUnit} > "$temp_dir/signal-cli-daemon.service"
          sed "s/^User=.*/User=$bot_user/" ${pollServiceUnit} > "$temp_dir/bot-poll.service"

          sudo install -m 0644 "$temp_dir/bot.service" /etc/systemd/system/bot.service
          sudo install -m 0644 "$temp_dir/signal-cli-daemon.service" /etc/systemd/system/signal-cli-daemon.service
          sudo install -m 0644 "$temp_dir/bot-poll.service" /etc/systemd/system/bot-poll.service
          sudo install -m 0644 ${pollTimerUnit} /etc/systemd/system/bot-poll.timer

          sudo systemctl daemon-reload
          sudo systemctl enable --now signal-cli-daemon.service
          sudo systemctl enable --now bot.service
          sudo systemctl enable --now bot-poll.timer

          echo "Installed and started:"
          echo " - signal-cli-daemon.service"
          echo " - bot.service"
          echo " - bot-poll.timer"
          echo " - user: $bot_user"
        '';
      };
    in {
      packages = {
        signal-cli = pkgs.signal-cli;
      };
      devShells.default = pkgs.mkShell {
        packages = [
          pkgs.bash
          pkgs.coreutils
          pkgs.git
          pkgs.python3
          pkgs.signal-cli
        ];
        shellHook = ''
          set -euo pipefail

          venv_dir="$PWD/.venv"
          if [ ! -d "$venv_dir" ]; then
            python -m venv "$venv_dir"
          fi

          # Activate venv
          # shellcheck disable=SC1091
          source "$venv_dir/bin/activate"

          python -m pip install -U pip wheel setuptools

          if [ -f requirements.txt ]; then
            python -m pip install -r requirements.txt
          fi

          if [ -f requirements-dev.txt ]; then
            python -m pip install -r requirements-dev.txt
          fi

          if [ -f .env ]; then
            set -a
            # shellcheck disable=SC1091
            source .env
            set +a
          fi
        '';
      };
      apps = {
        install = {
          type = "app";
          program = "${install}/bin/bot-install";
        };
        pollOnce = {
          type = "app";
          program = "${pollOnce}/bin/bot-poll-once";
        };
        run = {
          type = "app";
          program = "${runBot}/bin/bot-run";
        };
      };
    });
}
