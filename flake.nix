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

          if [ ! -d "${venvDir}" ]; then
            python -m venv "${venvDir}"
          fi

          # Activate venv
          source "${venvDir}/bin/activate"

          # Install deps (fast when already satisfied)
          if [ -f requirements.txt ]; then
            python -m pip install -r requirements.txt
          fi

          exec python -m bot
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

          # Restart the service; the service ExecStart will (re)install pip deps
          systemctl restart bot.service
          echo "Restarted bot.service"
        '';
      };

      serviceUnit = pkgs.writeText "bot.service" ''
        [Unit]
        Description=Signal bot (Nix runtime + pip venv)
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

        # Optional: if you want env vars (tokens, phone number, etc.)
        # EnvironmentFile=${repoDir}/.env

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
        runtimeInputs = [pkgs.bash pkgs.coreutils pkgs.systemd];
        text = ''
          set -euo pipefail

          sudo install -m 0644 ${serviceUnit} /etc/systemd/system/bot.service
          sudo install -m 0644 ${pollServiceUnit} /etc/systemd/system/bot-poll.service
          sudo install -m 0644 ${pollTimerUnit} /etc/systemd/system/bot-poll.timer

          sudo systemctl daemon-reload
          sudo systemctl enable --now bot.service
          sudo systemctl enable --now bot-poll.timer

          echo "Installed and started:"
          echo " - bot.service"
          echo " - bot-poll.timer"
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
          source "$venv_dir/bin/activate"

          python -m pip install -U pip wheel setuptools

          if [ -f requirements.txt ]; then
            python -m pip install -r requirements.txt
          fi

          if [ -f requirements-dev.txt ]; then
            python -m pip install -r requirements-dev.txt
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
