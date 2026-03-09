# logmedic

A Rust daemon that automatically detects high-frequency log errors and remediates them using AI.

## What it does

Production systems generate enormous volumes of logs. When something goes wrong, the same error lines fire thousands of times — burying signal in noise and burning through log storage. logmedic closes the loop:

1. **Detect** — Plugin-based detectors query log sources (Grafana Loki, etc.) for high-frequency error and warning patterns
2. **Diagnose** — AI analyzes the detected patterns, identifies root causes, and proposes concrete fixes
3. **Remediate** — Fixes are applied automatically: raising PRs against infrastructure repos (Ansible, Terraform, k8s manifests) or SSHing into hosts to apply changes directly

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 logmedic daemon                  │
│              (tokio async runtime)               │
├─────────────┬───────────────────┬───────────────┤
│   Config    │  Plugin Manager   │   Scheduler   │
│   (TOML)    │                   │  (poll loop)  │
│             ├─────────┬─────────┤               │
│             │ Native  │ Python  │               │
│             │ (dylib) │ (PyO3)  │               │
└─────────────┴─────────┴─────────┴───────────────┘
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
  ┌───────────┐          ┌──────────────┐
  │ Detectors │          │ Remediators  │
  ├───────────┤          ├──────────────┤
  │ Loki      │          │ Claude AI    │
  │ (more...) │          │ (more...)    │
  └─────┬─────┘          └──────┬───────┘
        │                       │
        ▼                       ▼
  LogAnomaly[]            RemediationAction[]
  - pattern               - Pull Request
  - count                 - SSH Command
  - level                 - Report
  - labels
  - samples
```

## Plugins

### Detectors

Detectors find high-frequency log patterns. They implement the `Detector` trait and return `LogAnomaly` results.

**Loki Detector** (`plugins/loki_detector/`) — Queries Grafana Loki via LogQL, normalizes log lines (collapsing UUIDs, IPs, timestamps into placeholders), groups by pattern, and surfaces lines exceeding the frequency threshold.

### Remediators

Remediators take anomalies and fix them. They implement the `Remediator` trait with `propose()` and `execute()` methods.

**Claude Remediator** (`plugins/claude_remediator/`) — Sends anomaly data to the Anthropic Claude API. Claude analyzes root causes and returns structured remediation actions:
- **Pull Requests** — Clones a repo, applies file changes, pushes a branch, and opens a PR via `gh` CLI
- **SSH Commands** — SSHes into the affected host and runs fix commands
- **Reports** — When automated action isn't appropriate, produces a diagnostic report

### Writing your own plugins

**Python plugins** — Create a `.py` file with a `DetectorPlugin` or `RemediatorPlugin` class. See the included plugins for the interface.

**Native plugins** — Build a Rust shared library (`.so`/`.dylib`) exporting `create_detector` or `create_remediator` functions that return `Box<dyn Detector>` or `Box<dyn Remediator>`.

## Configuration

```toml
[daemon]
poll_interval_secs = 300   # how often to run detection (5 min)
frequency_threshold = 50   # min occurrences to flag a pattern
lookback = "1h"            # time window for log queries

[[plugins]]
name = "loki"
kind = "python"
path = "plugins/loki_detector/loki_detector.py"

[plugins.settings]
loki_url = "http://localhost:3100"
# org_id = "tenant-1"
# extra_labels = '{namespace="production"}'

[[remediators]]
name = "claude"
kind = "ai"

[remediators.settings]
path = "plugins/claude_remediator/claude_remediator.py"
model = "claude-sonnet-4-20250514"
# anthropic_api_key = ""      # or set ANTHROPIC_API_KEY env var
# default_repo = "myorg/infra-ansible"
# ssh_key_path = "~/.ssh/id_ed25519"
# system_prompt = "Our infra uses Ansible. Services run on k8s in AWS."
```

## Building

```bash
cargo build --release
```

Requires:
- Rust 2021 edition
- Python 3.8+ (for PyO3 plugin embedding)
- `pkg-config` and Python development headers (`python3-dev` / `python3-devel`)

## Running

```bash
# With default config path (logmedic.toml)
./target/release/logmedic

# With custom config
./target/release/logmedic /etc/logmedic/config.toml

# With debug logging
RUST_LOG=logmedic=debug ./target/release/logmedic
```

## License

MIT
