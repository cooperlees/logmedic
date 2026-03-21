use serde::Deserialize;
use std::collections::HashMap;
use std::path::PathBuf;

use crate::error::Error;

/// Raw config as deserialized from TOML (uses maps keyed by plugin name).
#[derive(Debug, Deserialize, Clone)]
struct RawConfig {
    daemon: DaemonConfig,
    #[serde(default)]
    plugins: HashMap<String, RawPluginConfig>,
    #[serde(default)]
    remediators: HashMap<String, RawRemediatorConfig>,
}

/// Public config with name baked into each entry.
#[derive(Debug, Clone)]
pub struct Config {
    pub daemon: DaemonConfig,
    pub plugins: Vec<PluginConfig>,
    pub remediators: Vec<RemediatorConfig>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct DaemonConfig {
    /// How often to run detection plugins (seconds)
    #[serde(default = "default_poll_interval")]
    pub poll_interval_secs: u64,
    /// Minimum occurrences to consider a log line "high frequency"
    #[serde(default = "default_threshold")]
    pub frequency_threshold: u64,
    /// Lookback window for log queries (e.g. "1h", "30m")
    #[serde(default = "default_lookback")]
    pub lookback: String,
    /// Port for Prometheus metrics HTTP endpoint
    #[serde(default = "default_metrics_port")]
    pub metrics_port: u16,
    /// Deny list: anomalies whose labels contain any matching "key=value" entry are skipped.
    /// Example: ["app=homeassistant", "namespace=legacy"] ignores all anomalies from those sources.
    #[serde(default)]
    pub deny_labels: Vec<String>,
}

/// TOML shape: `[plugins.<name>]` with `kind`, `path`, and arbitrary settings.
#[derive(Debug, Deserialize, Clone)]
struct RawPluginConfig {
    kind: PluginKind,
    path: PathBuf,
    /// All remaining keys become plugin-specific settings.
    #[serde(flatten)]
    settings: toml::Table,
}

/// TOML shape: `[remediators.<name>]` with `kind` and arbitrary settings.
#[derive(Debug, Deserialize, Clone)]
struct RawRemediatorConfig {
    kind: RemediatorKind,
    /// All remaining keys become remediator-specific settings.
    #[serde(flatten)]
    settings: toml::Table,
}

#[derive(Debug, Clone)]
pub struct PluginConfig {
    pub name: String,
    pub kind: PluginKind,
    pub path: PathBuf,
    /// Plugin-specific settings passed as key-value pairs
    pub settings: toml::Table,
}

#[derive(Debug, Clone)]
pub struct RemediatorConfig {
    pub name: String,
    pub kind: RemediatorKind,
    /// Remediator-specific settings
    pub settings: toml::Table,
}

#[derive(Debug, Deserialize, Clone)]
#[serde(rename_all = "snake_case")]
pub enum PluginKind {
    Native,
    Python,
}

#[derive(Debug, Deserialize, Clone)]
#[serde(rename_all = "snake_case")]
pub enum RemediatorKind {
    /// AI-powered remediator (Claude, etc.)
    Ai,
    /// Script-based remediator
    Script,
}

fn default_poll_interval() -> u64 {
    300
}

fn default_threshold() -> u64 {
    100
}

fn default_lookback() -> String {
    "1h".to_string()
}

fn default_metrics_port() -> u16 {
    6969
}

pub fn load_config(path: &str) -> std::result::Result<Config, Error> {
    let content = std::fs::read_to_string(path).map_err(|e| Error::ConfigLoad {
        path: path.to_string(),
        source: Box::new(e),
    })?;
    let raw: RawConfig = toml::from_str(&content).map_err(|e| Error::ConfigParse {
        path: path.to_string(),
        source: e,
    })?;

    let plugins = raw
        .plugins
        .into_iter()
        .map(|(name, raw)| PluginConfig {
            name,
            kind: raw.kind,
            path: raw.path,
            settings: raw.settings,
        })
        .collect();

    let remediators = raw
        .remediators
        .into_iter()
        .map(|(name, raw)| RemediatorConfig {
            name,
            kind: raw.kind,
            settings: raw.settings,
        })
        .collect();

    Ok(Config {
        daemon: raw.daemon,
        plugins,
        remediators,
    })
}
