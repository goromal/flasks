# flasks

Python sources for the Flask web apps packaged and deployed by
[anixpkgs](https://github.com/goromal/anixpkgs), which consumes this repo as a
non-flake input (`flasks.url = "github:goromal/flasks"`). Each top-level
directory is one app; the Nix packaging (`default.nix`) and NixOS service
modules (`module.nix`) live in anixpkgs under `pkgs/python-packages/flasks/`.

| App | Description |
| --- | --- |
| `anix-upgrade-ui` | Web UI for triggering anix-upgrade |
| `authui` | Interface for remotely refreshing credentials |
| `budget_ui` | Interface for doing the budget |
| `cozy` | One-pager UI for generating images with ComfyUI workflows |
| `disciple` | Book of Mormon Christ-reference study tool |
| `intake_ui` | UI for sending goromail messages |
| `la-quiz-web` | Web-based LA geography quiz game |
| `orchestrator_ui` | Web UI for managing orchestrator jobs |
| `rankserver` | Webserver for ranking files via binary manual comparisons |
| `stampserver` | Interface for stamping metadata on PNGs and MP4s |
| `sunset` | Web UI to show and force-kill the running Dolphin emulator |
| `tasks_ui` | Flask UI for task-tools |
| `tester` | Self-testing and exam tool |
| `videodl` | Web UI for downloading videos via yt-dlp |

To iterate locally against anixpkgs without pushing:

```bash
nix build /path/to/anixpkgs#<app> --override-input flasks path:/path/to/flasks
```
