# logmedic

A Rust daemon that automatically detects high-frequency log errors and remediates them using AI.

## The Log Medic's Mantra

- **C**ontext is king; find the timestamp and the trace.
- **O**pen the config to see what’s out of place.
- **O**utput streams filtered to find the root cause.
- **P**atch up the logic and pause for the flaws.
- **E**liminate bugs that the console displayed.
- **R**un it again once the fixes are made.

## What it does

Production systems generate enormous volumes of logs. When something goes wrong, the same error lines fire thousands of times — burying signal in noise and burning through log storage. logmedic closes the loop:

1. **Detect** — Plugin-based detectors query log sources (Grafana Loki, etc.) for high-frequency error and warning patterns
2. **Diagnose** — AI analyzes the detected patterns, identifies root causes, and proposes concrete fixes
3. **Remediate** — Fixes are applied automatically: raising PRs against infrastructure repos (Ansible, Terraform, k8s manifests) or SSHing into hosts to apply changes directly

## Quick Start

### 1. Get your API tokens

**Anthropic API key** (for Claude-powered remediation):

1. Sign up or log in at [console.anthropic.com](https://console.anthropic.com/)
2. Go to **Settings > API Keys**
3. Click **Create Key**, give it a name, and copy the key (starts with `sk-ant-`)

**GitHub personal access token** (for creating pull requests):

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Click **Generate new token (classic)**
3. Select scopes: `repo` (full control of private repositories)
4. Click **Generate token** and copy it

### 2. Create a minimal config

Create a `logmedic.toml` file:

```toml
[daemon]
poll_interval_secs = 300
frequency_threshold = 50
lookback = "1h"

[plugins.loki]
kind = "python"
path = "plugins/loki_detector/loki_detector.py"
loki_url = "http://loki:3100"    # adjust to your Loki endpoint

[remediators.claude]
kind = "ai"
path = "plugins/claude_remediator/claude_remediator.py"
model = "claude-opus-4-6"
default_repo = "myorg/infra"     # repo to open PRs against
```

### 3. Run with Docker

```bash
docker run --rm \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -e GITHUB_TOKEN="ghp_..." \
  -v $(pwd)/logmedic.toml:/config.toml \
  cooperlees/logmedic:latest /config.toml
```

The health endpoint is available at `http://localhost:6969/healthz` — add `-p 6969:6969` if you want to expose it:

```bash
docker run --rm \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -e GITHUB_TOKEN="ghp_..." \
  -p 6969:6969 \
  -v $(pwd)/logmedic.toml:/config.toml \
  cooperlees/logmedic:latest /config.toml
```

For debugging, use the Debian-based image which includes a shell:

```bash
docker run --rm -it \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -e GITHUB_TOKEN="ghp_..." \
  -v $(pwd)/logmedic.toml:/config.toml \
  cooperlees/logmedic:latest-ubuntu bash
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 logmedic daemon                 │
│              (tokio async runtime)              │
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

logmedic supports Python plugins and native shared-library plugins.

#### Python example (detector)

Create a Python module with a `DetectorPlugin` class:

```python
import json

class DetectorPlugin:
    def __init__(self, settings: dict):
        # logmedic passes plugin settings as JSON in settings['settings_json']
        cfg = json.loads(settings.get("settings_json", "{}"))
        self.pattern = cfg.get("pattern", "ERROR")

    def detect(self, lookback: str, threshold: int):
        # Return list[dict] with at least "pattern" and "count"
        return [{"pattern": self.pattern, "count": threshold + 1}]
```

Configure it:

```toml
[plugins.simple_python]
kind = "python"
path = "/opt/logmedic/plugins/simple_detector.py"
pattern = "database timeout" # non-reserved keys (everything except kind/path) are in settings_json
```

#### Python plugin dependencies (PyPI packages)

logmedic embeds CPython via PyO3. The embedded interpreter uses the **system Python 3.13's site-packages**, so any third-party packages your plugin imports must be installed into that interpreter before logmedic starts.

Declare your plugin's dependencies in a `pyproject.toml` file alongside your plugin. Using `pyproject.toml` is preferred over `requirements.txt` because it supports metadata, version constraints, and works with all modern Python tooling:

```toml
# plugins/my_detector/pyproject.toml
[project]
name = "my-detector"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "httpx>=0.27",
    "structlog>=24.0",
]
```

**Docker (recommended):** extend the official Ubuntu image and install your deps at build time:

```dockerfile
FROM cooperlees/logmedic:latest-ubuntu

# Install python3-pip once, then install your plugin as a package
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3-pip && \
    rm -rf /var/lib/apt/lists/*
COPY plugins/ /plugins/
RUN python3.13 -m pip install --break-system-packages /plugins/my_detector/

ENTRYPOINT ["/logmedic"]
```

**Bare metal:** install deps into the same Python 3.13 that logmedic was compiled against:

```bash
python3.13 -m pip install plugins/my_detector/
```

> The distroless image (`cooperlees/logmedic:latest`) is built without Python support (`--no-default-features`) and cannot run Python plugins. Use the Ubuntu image as the base when Python plugins are needed.

#### Rust example (native detector)

Create a `cdylib` that exports `create_detector`:

```rust
use async_trait::async_trait;
use logmedic::detect::{Detector, LogAnomaly, LogLevel};
use logmedic::error::PluginError;

struct SimpleDetector;

#[async_trait]
impl Detector for SimpleDetector {
    fn name(&self) -> &str { "simple-native" }

    async fn detect(&self, _lookback: &str, threshold: u64) -> Result<Vec<LogAnomaly>, PluginError> {
        Ok(vec![LogAnomaly {
            pattern: "native example".to_string(),
            count: threshold + 1,
            level: LogLevel::Error,
            labels: Default::default(),
            samples: vec!["example log line".to_string()],
        }])
    }
}

#[no_mangle]
pub unsafe fn create_detector(_settings: &str) -> Box<dyn Detector> {
    Box::new(SimpleDetector)
}
```

Configure it:

```toml
[plugins.simple_native]
kind = "native"
path = "/opt/logmedic/plugins/libsimple_detector.so" # .dylib on macOS
```

#### Using plugins from another repository

This is supported today: plugin paths can point anywhere on disk, including another local git checkout.

```toml
[plugins.shared_team_detector]
kind = "python"
path = "/opt/plugins/logmedic-plugins/python/team_detector.py"
```

```toml
[plugins.shared_team_native]
kind = "native"
path = "/opt/plugins/logmedic-plugins/target/release/libteam_detector.so"
```

For remediators, set `path` on the remediator table in the same way:

```toml
[remediators.shared_team_fixer]
kind = "ai"
path = "/opt/plugins/logmedic-plugins/python/team_remediator.py"
```

Note: logmedic does not clone git repositories automatically. Clone/sync external plugin repos separately, then reference their plugin files with absolute paths (for example, either `git clone https://github.com/myorg/logmedic-plugins /opt/plugins/logmedic-plugins` or `git clone git@github.com:myorg/logmedic-plugins.git /opt/plugins/logmedic-plugins`, depending on your auth method).

## Configuration

```toml
[daemon]
poll_interval_secs = 300   # how often to run detection (5 min)
frequency_threshold = 50   # min occurrences to flag a pattern
lookback = "1h"            # time window for log queries
metrics_port = 6969        # Prometheus /metrics endpoint

[plugins.loki]
kind = "python"
path = "plugins/loki_detector/loki_detector.py"
loki_url = "http://localhost:3100"
# org_id = "tenant-1"
# extra_labels = '{namespace="production"}'
# deny_labels = ["app=homeassistant", "namespace=legacy"]  # skip anomalies from these Loki stream labels

[remediators.claude]
kind = "ai"
path = "plugins/claude_remediator/claude_remediator.py"
model = "claude-opus-4-6"
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
- Python 3.9–3.13 (for PyO3 plugin embedding; 3.14 not yet supported — see [PyO3#4584](https://github.com/PyO3/pyo3/issues/4584))
- `pkg-config` and Python development headers (`python3-dev` / `python3-devel`)

**Python version policy:** logmedic aggressively tracks the latest Python release. Each logmedic release will target the newest Python version supported by PyO3. Plugin authors should write modern Python and avoid deprecated features — if the latest stable CPython supports it, use it.

## Docker

Pre-built multi-arch images (`linux/amd64` + `linux/arm64`) are pushed to Docker Hub on every merge to `main` and on version tags.

| Tag | Base image | Description |
|-----|-----------|-------------|
| `cooperlees/logmedic:latest` | `gcr.io/distroless/cc-debian12` | Minimal distroless image (no shell). Recommended for production. |
| `cooperlees/logmedic:v1.2.3` | `gcr.io/distroless/cc-debian12` | Pinned release — distroless. |
| `cooperlees/logmedic:latest-ubuntu` | `ubuntu:24.04` | Ubuntu image with bash/shell for debugging. |
| `cooperlees/logmedic:v1.2.3-ubuntu` | `ubuntu:24.04` | Pinned release — Ubuntu. |

```bash
# Minimal distroless (recommended)
docker run --rm -v $(pwd)/logmedic.toml:/config.toml cooperlees/logmedic:latest /config.toml

# Debian slim (has shell for debugging)
docker run --rm -it -v $(pwd)/logmedic.toml:/config.toml cooperlees/logmedic:latest-ubuntu bash
```

## Kubernetes (Helm)

A Helm chart is available at `charts/logmedic`.

```bash
# install with default values
helm install logmedic ./charts/logmedic

# or provide custom values (recommended)
helm install logmedic ./charts/logmedic -f my-values.yaml
```

By default, the chart uses conservative resources:
- requests: `25m` CPU / `64Mi` memory
- limits: `100m` CPU / `256Mi` memory

### High availability mode (active/passive only)

Today, Kubernetes support is intentionally limited to **active/passive**. Horizontal active/active scaling and sharded work distribution are not supported yet.

Enable active/passive with two replicas:

```yaml
activePassive:
  enabled: true
  replicas: 2
```

What this does:
- Uses a Kubernetes `Lease` object for leader election.
- Runs exactly one active leader loop at a time.
- Keeps one passive standby pod ready to take over if the active pod/node is drained or lost.
- Prefers spreading the two pods onto different nodes (`kubernetes.io/hostname`) by default.
- Requires the Ubuntu image variant (Python-enabled) for the built-in leader-election wrapper.
- If you set a custom `.Values.affinity`, that custom affinity overrides the default node-spread preference.

Current limitations:
- Runtime processing state is not shared between pods; failover starts from the new leader’s in-memory state.
- Prometheus metrics are process-local; after failover, counters/histograms continue from the newly active pod’s local metrics state.
- Active/active and workload sharding are future work.

To provide API tokens, either reference an existing secret:

```yaml
secrets:
  name: logmedic-api-keys
```

or let Helm create one (not recommended for production — see warning below):

```yaml
secrets:
  create: true
  anthropicApiKey: "sk-ant-..."
  githubToken: "ghp_..."
```

> **⚠️ Security warning:** Helm stores all release values (including any tokens you pass via
> `secrets.anthropicApiKey` / `secrets.githubToken`) in an in-cluster Secret and they may also
> end up in GitOps repositories or CI logs. For production deployments, pre-create the Secret
> outside of Helm and reference it with `secrets.name` instead of using `secrets.create`.

Then verify health:

```bash
kubectl port-forward svc/logmedic-logmedic 6969:6969
curl -fsS http://127.0.0.1:6969/healthz
```

In active/passive mode, the service routes to the active pod only (readiness is leader-gated).

## Running

```bash
# With default config path (logmedic.toml)
./target/release/logmedic

# With custom config
./target/release/logmedic /etc/logmedic/config.toml

# With debug logging
RUST_LOG=logmedic=debug ./target/release/logmedic
```

## Metrics

logmedic runs an HTTP server (default port 6969) with two endpoints:

- **`/healthz`** — Returns `200` if all plugins loaded successfully, `503` otherwise. JSON body shows expected vs loaded counts for detectors and remediators.
- **`/metrics`** — Prometheus-compatible metrics endpoint. Scrape it with your existing Prometheus instance.

> In Kubernetes active/passive mode, metrics are emitted by the currently active leader pod and are not globally shared across replicas. Use Prometheus recording rules if you need continuity across failover events.

Exposed metrics:

| Metric | Type | Description |
|--------|------|-------------|
| `logmedic_detectors_loaded` | gauge | Number of detector plugins loaded |
| `logmedic_remediators_loaded` | gauge | Number of remediator plugins loaded |
| `logmedic_detection_cycles_total` | counter | Total detection cycles run |
| `logmedic_detection_cycle_duration_seconds` | histogram | Duration of each full cycle |
| `logmedic_detector_runs_total` | counter | Runs per detector (label: `detector`) |
| `logmedic_detector_errors_total` | counter | Errors per detector (label: `detector`) |
| `logmedic_anomalies_detected_total` | counter | Anomalies found per detector |
| `logmedic_anomalies_per_cycle` | gauge | Anomalies in the most recent cycle per detector |
| `logmedic_anomalies_by_level_total` | counter | Anomalies by severity (label: `level`) |
| `logmedic_remediations_proposed_total` | counter | Actions proposed per remediator |
| `logmedic_remediations_executed_total` | counter | Actions executed (labels: `remediator`, `status`) |
| `logmedic_remediation_errors_total` | counter | Execution errors per remediator |
| `logmedic_remediation_duration_seconds` | histogram | Execution duration per remediator |
| `logmedic_remediation_actions_by_kind_total` | counter | Actions by kind (`pull_request`, `ssh_command`, `report`) |
| `logmedic_daemon_start_time_seconds` | gauge | Unix timestamp of daemon start |

Example Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: logmedic
    static_configs:
      - targets: ['localhost:6969']
```

## License

MIT
