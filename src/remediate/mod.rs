use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::detect::LogAnomaly;
use crate::error::PluginError;

/// An action the remediator wants to take.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RemediationAction {
    pub description: String,
    pub kind: ActionKind,
    pub status: ActionStatus,
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
    SshCommand {
        host: String,
        commands: Vec<String>,
    },
    /// Just report — no automated action
    Report {
        message: String,
    },
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
