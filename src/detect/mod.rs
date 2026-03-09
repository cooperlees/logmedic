use async_trait::async_trait;
use serde::{Deserialize, Serialize};

/// A high-frequency log pattern detected by a plugin.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogAnomaly {
    /// The log line pattern or template
    pub pattern: String,
    /// How many times it occurred in the lookback window
    pub count: u64,
    /// Log level (error, warn, etc.)
    pub level: LogLevel,
    /// Source labels (e.g. service name, namespace, host)
    pub labels: std::collections::HashMap<String, String>,
    /// A few sample log lines matching this pattern
    pub samples: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LogLevel {
    Error,
    Warn,
    Unknown,
}

/// Trait that all detector plugins must implement.
/// Native plugins export a `create_detector` fn returning Box<dyn Detector>.
/// Python plugins implement a class with the same method signatures.
#[async_trait]
pub trait Detector: Send + Sync {
    /// Human-readable name of this detector
    fn name(&self) -> &str;

    /// Run detection and return any anomalies found.
    async fn detect(
        &self,
        lookback: &str,
        threshold: u64,
    ) -> anyhow::Result<Vec<LogAnomaly>>;
}
