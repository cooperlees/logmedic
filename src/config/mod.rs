use serde::Deserialize;
use std::path::PathBuf;

#[derive(Debug, Deserialize, Clone)]
pub struct Config {
    pub daemon: DaemonConfig,
    #[serde(default)]
    pub plugins: Vec<PluginConfig>,
    #[serde(default)]
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
}

#[derive(Debug, Deserialize, Clone)]
pub struct PluginConfig {
    pub name: String,
    pub kind: PluginKind,
    pub path: PathBuf,
    /// Plugin-specific settings passed as key-value pairs
    #[serde(default)]
    pub settings: toml::Table,
}

#[derive(Debug, Deserialize, Clone)]
pub struct RemediatorConfig {
    pub name: String,
    pub kind: RemediatorKind,
    /// Remediator-specific settings
    #[serde(default)]
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

pub fn load_config(path: &str) -> anyhow::Result<Config> {
    let content = std::fs::read_to_string(path)?;
    let config: Config = toml::from_str(&content)?;
    Ok(config)
}
