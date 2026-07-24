{
  description = "Vegan Activsts NL Signal bot";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    pre-commit-hooks.url = "github:cachix/git-hooks.nix";
  };

  outputs = {
    self,
    nixpkgs,
    flake-utils,
    pre-commit-hooks,
  }:
    flake-utils.lib.eachDefaultSystem (system: let
      pkgs = import nixpkgs {inherit system;};
      nixPython = "${pkgs.python312}/bin/python";

      runtimePkgs = with pkgs; [
        git
        nix
        nushell
        python312
        signal-cli
        uv
      ];

      nuShellScript = ''
        #!${pkgs.nushell}/bin/nu

        def required_flags [flags: list<record>] {
          mut msgs: list<string> = []
          for flag in $flags {
            if ($flag.value | is-empty) {
              let env_msg = if ($flag | get -o env) != null { $" or use environment variable $($flag.env)" } else { ""}
              $msgs = ($msgs | append $"Error: Missing required flag: --($flag.name)($env_msg)")
            }
          }
          if ($msgs | length) > 0 {
            print ($msgs | str join (char nl))
            exit 1
          }
        }
      '';

      runBot = pkgs.writeScriptBin "bot-run" ''
        ${nuShellScript}

        def main [
          --repo-dir: string = "."
          --config: string
        ] {
          required_flags [
            { name: "config", value: $config }
          ]
          let repo_dir = ($repo_dir | path expand)

          let tmp_dir = ($repo_dir | path join "tmp")
          let run_dir = ($repo_dir | path join "run")
          let signal_socket_path = ($run_dir | path join "signal-cli.sock")

          mkdir $tmp_dir
          $env.TMPDIR = $tmp_dir
          $env.JAVA_TOOL_OPTIONS = $"-Djava.io.tmpdir=($tmp_dir)"
          $env.UV_NO_MANAGED_PYTHON = "1"
          $env.UV_PYTHON = "${nixPython}"

          cd $repo_dir
          ^${pkgs.uv}/bin/uv run --frozen --python "${nixPython}" bot --config $config
        }
      '';

      runSignalDaemon = pkgs.writeScriptBin "signal-daemon-run" ''
        ${nuShellScript}

        def main [
          # Signal account number for signal-cli (or use environment variable $SIGNAL_ACCOUNT)
          --signal-account: string
          # Repository root to install from
          --repo-dir: string = "."
          # 0 = non-verbose, 1 = verbose, 2 = extra verbose
          --verbose-level: int = 0
          # optional signal-cli log file path
          --log-file: string = ""
        ] {
          let signal_account = ($signal_account | default ($env | get -o SIGNAL_ACCOUNT))
          required_flags [
            { name: "signal-account", value: $signal_account, env: "SIGNAL_ACCOUNT" }
          ]
          let repo_dir = ($repo_dir | path expand)

          let tmp_dir = ($repo_dir | path join "tmp")
          let run_dir = ($repo_dir | path join "run")
          let signal_socket_path = ($run_dir | path join "signal-cli.sock")

          mkdir $tmp_dir $run_dir
          if ($signal_socket_path | path exists) {
            rm $signal_socket_path
          }
          $env.TMPDIR = $tmp_dir
          $env.JAVA_TOOL_OPTIONS = $"-Djava.io.tmpdir=($tmp_dir)"

          let verbose_args = if $verbose_level == 2 {
            ["-vv"]
          } else if $verbose_level == 1 {
            ["-v"]
          } else if $verbose_level == 0 {
            []
          } else {
            print -e $"Invalid verbose-level value: ($verbose_level)"
            print -e "Expected 0, 1, or 2"
            exit 1
          }

          let log_file_args = if $log_file == "" { [] } else { ["--log-file" $log_file] }

          cd $repo_dir
          ^${pkgs.signal-cli}/bin/signal-cli ...$verbose_args ...$log_file_args -u $signal_account daemon --socket $signal_socket_path --receive-mode on-connection
        }
      '';

      pollOnce = pkgs.writeScriptBin "bot-poll-once" ''
        ${nuShellScript}

        def main [
          --repo-dir: string = "."
          --bot-user: string
        ] {
          required_flags [
            { name: "repo-dir", value: $repo_dir }
            { name: "bot-user", value: $bot_user }
          ]
          let repo_dir = ($repo_dir | path expand)

          let selected_bot_user = if $bot_user == "" { $env.USER } else { $bot_user }
          let remote = "origin"
          let branch = "main"

          let fetch_result = (^${pkgs.util-linux}/bin/runuser -u $selected_bot_user -- ${pkgs.git}/bin/git -C $repo_dir fetch $remote $branch | complete)
          if $fetch_result.exit_code != 0 {
            if $fetch_result.stderr != "" {
              print -e $fetch_result.stderr
            }
            exit $fetch_result.exit_code
          }

          let local_rev = (^${pkgs.util-linux}/bin/runuser -u $selected_bot_user -- ${pkgs.git}/bin/git -C $repo_dir rev-parse HEAD | str trim)
          let remote_rev = (^${pkgs.util-linux}/bin/runuser -u $selected_bot_user -- ${pkgs.git}/bin/git -C $repo_dir rev-parse $"($remote)/($branch)" | str trim)

          if $local_rev == $remote_rev {
            print $"No changes (($local_rev))."
            exit 0
          }

          print $"Updating: ($local_rev) -> ($remote_rev)"
          let reset_result = (^${pkgs.util-linux}/bin/runuser -u $selected_bot_user -- ${pkgs.git}/bin/git -C $repo_dir reset --hard $"($remote)/($branch)" | complete)
          if $reset_result.exit_code != 0 {
            if $reset_result.stderr != "" {
              print -e $reset_result.stderr
            }
            exit $reset_result.exit_code
          }

          ^systemctl restart signal-daemon.service
          ^systemctl restart bot.service
          print "Restarted signal-daemon.service and bot.service"
        }
      '';

      install = pkgs.writeScriptBin "bot-install" ''
        ${nuShellScript}

        def render-unit [lines] {
          $lines | str join (char nl)
        }

        def main [
          # Signal account number for signal-cli (or use environment variable $SIGNAL_ACCOUNT)
          --signal-account: string
          # Repository root to install from
          --repo-dir: string = "."
          # The path to the config file for the bot
          --config: string
          # Install runtime units instead of persistent units (for testing)
          --runtime
        ] {
          let signal_account = ($signal_account | default ($env | get -o SIGNAL_ACCOUNT))
          required_flags [
            { name: "signal-account", value: $signal_account, env: "SIGNAL_ACCOUNT" }
            { name: "config", value: $config }
          ]

          # Expand these paths, as systemd does not handle relative paths
          let repo_dir = ($repo_dir | path expand)
          let config = ($config | path expand)

          let bot_user = (($env | get --optional SUDO_USER) | default $env.USER)

          let bot_service = (render-unit [
            "[Unit]"
            "Description=Vegan Activists NL Signal bot"
            "Requires=signal-daemon.service"
            "After=signal-daemon.service"
            "After=network-online.target"
            "Wants=network-online.target"
            ""
            "[Service]"
            "Type=simple"
            $"User=($bot_user)"
            $"WorkingDirectory=($repo_dir)"
            $"ExecStart=${runBot}/bin/bot-run --repo-dir ($repo_dir) --config ($config)"
            ""
            "Restart=always"
            "RestartSec=2"
            "StandardOutput=journal"
            "StandardError=journal"
            ""
            "[Install]"
            "WantedBy=multi-user.target"
          ])

          let daemon_service = (render-unit [
            "[Unit]"
            "Description=signal-cli daemon (JSON-RPC over Unix socket)"
            "After=network-online.target"
            "Wants=network-online.target"
            ""
            "[Service]"
            "Type=simple"
            $"User=($bot_user)"
            $"WorkingDirectory=($repo_dir)"
            $"ExecStart=${runSignalDaemon}/bin/signal-daemon-run --repo-dir ($repo_dir) --signal-account ($signal_account)"
            ""
            "Restart=always"
            "RestartSec=2"
            "StandardOutput=journal"
            "StandardError=journal"
            ""
            "[Install]"
            "WantedBy=multi-user.target"
          ])

          let poll_service = (render-unit [
            "[Unit]"
            "Description=Poll git main and restart Signal bot"
            ""
            "[Service]"
            "Type=oneshot"
            $"WorkingDirectory=($repo_dir)"
            $"ExecStart=${pollOnce}/bin/bot-poll-once --repo-dir ($repo_dir) --bot-user ($bot_user)"
          ])

          let poll_timer = (render-unit [
            "[Unit]"
            "Description=Poll git main every minute for Signal bot"
            ""
            "[Timer]"
            "OnBootSec=30"
            "OnUnitActiveSec=60"
            "Persistent=true"
            ""
            "[Install]"
            "WantedBy=timers.target"
          ])

          # Write systemd unit definitions to tmp files
          let tmp_dir = (mktemp -d | str trim)
          mkdir $tmp_dir
          $bot_service | save -f ($tmp_dir | path join "bot.service")
          $daemon_service | save -f ($tmp_dir | path join "signal-daemon.service")
          $poll_service | save -f ($tmp_dir | path join "bot-poll.service")
          $poll_timer | save -f ($tmp_dir | path join "bot-poll.timer")

          let systemd_unit_dir = if $runtime {
            "/run/systemd/system"
          } else {
            "/etc/systemd/system"
          }
          sudo install -d $systemd_unit_dir
          sudo install -m 0644 ($tmp_dir | path join "bot.service") ($systemd_unit_dir | path join "bot.service")
          sudo install -m 0644 ($tmp_dir | path join "signal-daemon.service") ($systemd_unit_dir | path join "signal-daemon.service")
          sudo install -m 0644 ($tmp_dir | path join "bot-poll.service") ($systemd_unit_dir | path join "bot-poll.service")
          sudo install -m 0644 ($tmp_dir | path join "bot-poll.timer") ($systemd_unit_dir | path join "bot-poll.timer")

          sudo systemctl daemon-reload
          let runtime = if $runtime { ["--runtime"] } else { [] }
          sudo systemctl ...$runtime enable --now signal-daemon.service
          sudo systemctl ...$runtime enable --now bot.service
          sudo systemctl ...$runtime enable --now bot-poll.timer

          print "Installed and started:"
          print " - signal-daemon.service"
          print " - bot.service"
          print " - bot-poll.timer"
          print $" - user: ($bot_user)"
          print $" - unit dir: ($systemd_unit_dir)"
          if $systemd_unit_dir == "/run/systemd/system" {
            print " - note: /etc/systemd/system is not writable, so units were installed as runtime units"
          }
        }
      '';

      uninstall = pkgs.writeScriptBin "bot-uninstall" ''
        ${nuShellScript}

        def main [] {
          let unit_dirs = ["/etc/systemd/system" "/run/systemd/system"]
          let units = [
            "bot.service"
            "bot-poll.service"
            "bot-poll.timer"
            "signal-daemon.service"
            "signal-cli-daemon.service" # legacy name
          ]

          for unit in $units {
            let result = (sudo systemctl disable --now $unit | complete)
            if $result.exit_code != 0 {
              let stderr = ($result.stderr | str trim)
              if $stderr != "" {
                print -e $stderr
              }
            }
          }

          for dir in $unit_dirs {
            for unit in $units {
              let unit_path = ($dir | path join $unit)
              if ($unit_path | path exists) {
                sudo rm $unit_path
              }
            }
          }

          sudo systemctl daemon-reload

          print "Uninstalled:"
          print " - bot.service"
          print " - bot-poll.service"
          print " - bot-poll.timer"
          print " - signal-daemon.service"
          print " - signal-cli-daemon.service (legacy)"
        }
      '';

      checkProject = pkgs.writeScriptBin "check-project" ''
        #!/usr/bin/env bash
        set -euo pipefail
        export UV_NO_MANAGED_PYTHON=1
        export UV_PYTHON="${nixPython}"
        ${pkgs.uv}/bin/uv run --dev --frozen --python "${nixPython}" pyrefly check .
        ${pkgs.uv}/bin/uv run --dev --frozen --python "${nixPython}" ruff check --fix
        ${pkgs.uv}/bin/uv run --dev --frozen --python "${nixPython}" pytest
      '';

      installPrecommitHooks = pkgs.writeScriptBin "install-precommit-hooks" ''
        #!/usr/bin/env bash
        ${self.checks.${system}.pre-commit-check.shellHook}
      '';

      link = pkgs.writeScriptBin "link" ''
        ${nuShellScript}
        def main [--machine-name: string] {
          required_flags [{ name: "machine-name", value: $machine_name }]
          let link_name = $"Vegan Activists NL bot (($machine_name))"
          ${pkgs.signal-cli}/bin/signal-cli link -n $link_name
        }
      '';
    in {
      packages = {
        default = pkgs.signal-cli;
        signal-cli = pkgs.signal-cli;
        install-precommit-hooks = installPrecommitHooks;
        check-project = checkProject;
      };

      devShells.default = pkgs.mkShell {
        packages = runtimePkgs ++ [self.packages.${system}.install-precommit-hooks];
        shellHook = ''
          export UV_NO_MANAGED_PYTHON=1
          export UV_PYTHON="${nixPython}"
          uv sync --dev --frozen --python "${nixPython}"
          source .venv/bin/activate
        '';
      };

      checks = {
        pre-commit-check = pre-commit-hooks.lib.${system}.run {
          src = ./.;
          hooks = {
            check-project = {
              name = "check-project";
              types = ["python"];
              enable = true;
              entry = "${self.packages.${system}.check-project}/bin/check-project";
            };
          };
        };
      };

      apps = {
        link = {
          type = "app";
          program = "${link}/bin/link";
        };
        install = {
          type = "app";
          program = "${install}/bin/bot-install";
        };
        poll-once = {
          type = "app";
          program = "${pollOnce}/bin/bot-poll-once";
        };
        bot = {
          type = "app";
          program = "${runBot}/bin/bot-run";
        };
        signal-daemon = {
          type = "app";
          program = "${runSignalDaemon}/bin/signal-daemon-run";
        };
        uninstall = {
          type = "app";
          program = "${uninstall}/bin/bot-uninstall";
        };
        install-precommit-hooks = {
          type = "app";
          program = "${self.packages.${system}.install-precommit-hooks}/bin/install-precommit-hooks";
        };
      };
    });
}
