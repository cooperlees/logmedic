use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::detect::LogAnomaly;
use crate::error::PluginError;

/// An action the remediator wants to take.
///
/// `context` carries the triggering [`LogAnomaly`]s end-to-end: Python
/// remediators return it from `propose()` (as `anomaly_context`) and read it
/// back in `execute()` for PR dedup and the appended log section. It defaults
/// to empty so actions constructed without context (e.g. in tests) keep
/// working; unknown plugin fields are ignored rather than rejected.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RemediationAction {
    pub description: String,
    pub kind: ActionKind,
    pub status: ActionStatus,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub context: Vec<LogAnomaly>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActionKind {
    /// Raise a PR against a repo (e.g. ansible, terraform, k8s manifests)
    PullRequest {
        repo: String,
        branch: String,
        title: String,
        body: String,
        files_changed: Vec<FileChange>,
    },
    /// SSH into a host and run commands
    SshCommand { host: String, commands: Vec<String> },
    /// Just report — no automated action
    Report { message: String },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileChange {
    pub path: String,
    pub content: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActionStatus {
    Proposed,
    Approved,
    Applied,
    Failed { reason: String },
}

/// Trait that all remediators must implement.
#[async_trait]
pub trait Remediator: Send + Sync {
    fn name(&self) -> &str;

    /// Given a set of anomalies, propose remediation actions.
    async fn propose(
        &self,
        anomalies: &[LogAnomaly],
    ) -> std::result::Result<Vec<RemediationAction>, PluginError>;

    /// Execute an approved action.
    async fn execute(
        &self,
        action: &RemediationAction,
    ) -> std::result::Result<ActionStatus, PluginError>;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_anomaly() -> LogAnomaly {
        LogAnomaly {
            pattern: "ERROR: disk full on <HOST>".to_string(),
            count: 87,
            level: crate::detect::LogLevel::Error,
            labels: [("host".to_string(), "vps-au1".to_string())]
                .into_iter()
                .collect(),
            samples: vec!["ERROR: disk full on vps-au1".to_string()],
        }
    }

    #[test]
    fn context_defaults_to_empty_and_round_trips() {
        // Actions built without context (tests, native plugins) keep working.
        let bare: RemediationAction = serde_json::from_str(
            r#"{"description":"d","kind":{"report":{"message":"m"}},"status":"proposed"}"#,
        )
        .unwrap();
        assert!(bare.context.is_empty());
        // Empty context is skipped on serialize (skip_serializing_if).
        assert!(!serde_json::to_string(&bare).unwrap().contains("context"));

        // Non-empty context survives a serialize -> deserialize round trip.
        let action = RemediationAction {
            description: "clean disk".to_string(),
            kind: ActionKind::Report {
                message: "disk full".to_string(),
            },
            status: ActionStatus::Proposed,
            context: vec![sample_anomaly()],
        };
        let back: RemediationAction =
            serde_json::from_str(&serde_json::to_string(&action).unwrap()).unwrap();
        assert_eq!(back.context.len(), 1);
        assert_eq!(back.context[0].pattern, "ERROR: disk full on <HOST>");
        assert_eq!(back.context[0].count, 87);
    }

    #[test]
    fn python_anomaly_context_key_maps_to_typed_context() {
        // Mirrors the call_propose bridge in src/plugin/python.rs: the plugin
        // attaches `anomaly_context`; the bridge moves it into `context`.
        let raw = serde_json::json!({
            "description": "d",
            "kind": {"report": {"message": "m"}},
            "status": "proposed",
            "anomaly_context": [
                {
                    "pattern": "ERROR: x",
                    "count": 3,
                    "level": "error",
                    "labels": {},
                    "samples": [],
                }
            ],
        });
        let context: Vec<LogAnomaly> = raw
            .get("anomaly_context")
            .and_then(|c| serde_json::from_value(c.clone()).ok())
            .unwrap_or_default();
        let mut action: RemediationAction = serde_json::from_value(raw).unwrap();
        action.context = context;
        assert_eq!(action.context.len(), 1);
        assert_eq!(action.context[0].count, 3);
    }
}
