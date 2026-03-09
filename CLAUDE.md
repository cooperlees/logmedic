# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
cargo build                        # Debug build (requires Python 3.13 + dev headers)
cargo build --release              # Optimized release build (strip, lto, codegen-units=1)
cargo test --all-features          # Run all tests
cargo clippy --all-targets --all-features  # Lint (CI treats warnings as errors via RUSTFLAGS="-D warnings")
cargo fmt --all --check            # Check formatting
cargo fmt --all                    # Auto-format
```

Python 3.13 must be installed with development headers (`python3-dev`). PyO3 0.22 does **not** support Python 3.14 yet ([PyO3#4584](https://github.com/PyO3/pyo3/issues/4584)). Set `PYO3_PYTHON=python3.13` if the system default differs. On Linux, the linker may need `LIBRARY_PATH` pointing to Python's lib directory.

## Architecture

**Three-phase async loop** running on tokio:

1. **Detect** — All `Detector` plugins query log sources, return `Vec<LogAnomaly>` (pattern, count, level, labels, samples)
2. **Propose** — All `Remediator` plugins analyze anomalies, return `Vec<RemediationAction>` (PullRequest | SshCommand | Report)
3. **Execute** — Each proposed action is executed, returning `ActionStatus`

**Plugin system** — Dual-backend, trait-based:
- **Native** (`src/plugin/native.rs`): Loads `.so`/`.dylib` via `libloading`, calls `create_detector`/`create_remediator` symbols. Libraries are intentionally leaked to stay loaded.
- **Python** (`src/plugin/python.rs`): Embeds Python via PyO3. Expects `DetectorPlugin` or `RemediatorPlugin` classes. All Python calls run in `spawn_blocking` to avoid blocking the async runtime on the GIL. Settings passed as JSON string inside a dict with key `settings_json`.

**Error types** (`src/error.rs`): `Error` (top-level) and `PluginError` (plugin-specific). Every `PluginError` variant includes hint messages to help plugin authors debug. Python errors capture full tracebacks via `format_python_error()`. Python error string fields use `detail: String` (not `source:`) to avoid thiserror's `#[source]` inference on non-Error types.

**Observability** (`src/metrics.rs`): Single HTTP server on `metrics_port` (default 6969) serves `/metrics` (Prometheus text format) and `/healthz` (JSON with expected vs loaded plugin counts, returns 503 if unhealthy).

**Config** (`src/config/mod.rs`): TOML-based. `[daemon]` for poll interval/threshold/lookback/port, `[[plugins]]` for detectors, `[[remediators]]` for remediators. Plugin settings are arbitrary `toml::Table` values serialized to JSON for plugins.

## CI

GitHub Actions (`.github/workflows/ci.yml`): fmt → clippy → test → build-release (x86_64 Linux, aarch64 Linux on ARM runner, aarch64 macOS, x86_64 Windows). All jobs pin Python 3.13.
