mod config;
mod detect;
mod error;
mod metrics;
mod plugin;
mod remediate;

use std::time::{Duration, Instant};
use tracing::{debug, error, info, warn};

use crate::remediate::ActionKind;

/// Filter out anomalies whose labels match any entry in the deny list.
///
/// Each deny entry must be a `"key=value"` string. An anomaly is excluded when
/// at least one of its label key-value pairs matches at least one deny entry.
/// Malformed entries (missing `=`) are silently skipped.
fn filter_denied_anomalies(
    anomalies: Vec<detect::LogAnomaly>,
    deny_labels: &[String],
) -> Vec<detect::LogAnomaly> {
    if deny_labels.is_empty() {
        return anomalies;
    }
    anomalies
        .into_iter()
        .filter(|anomaly| {
            !deny_labels.iter().any(|deny| {
                if let Some((k, v)) = deny.split_once('=') {
                    anomaly.labels.get(k).is_some_and(|lv| lv == v)
                } else {
                    warn!(entry = %deny, "deny_labels entry has no '=' separator, skipping");
                    false
                }
            })
        })
        .collect()
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| {
                "logmedic=info"
                    .parse()
                    .expect("hard-coded log filter 'logmedic=info' should always parse")
            }),
        )
        .init();

    let config_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "logmedic.toml".to_string());

    info!(config = %config_path, "starting logmedic daemon");
    debug!("loading configuration from {config_path}");
    let cfg = config::load_config(&config_path)?;
    debug!(
        poll_interval = cfg.daemon.poll_interval_secs,
        threshold = cfg.daemon.frequency_threshold,
        lookback = %cfg.daemon.lookback,
        metrics_port = cfg.daemon.metrics_port,
        detectors = cfg.plugins.len(),
        remediators = cfg.remediators.len(),
        "configuration loaded"
    );

    // Initialize metrics
    let m = metrics::Metrics::new()?;
    m.daemon_start_time.set(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("system clock is before UNIX epoch")
            .as_secs_f64(),
    );

    // Initialize health state
    let health = metrics::Health::new();
    health.set_expected(cfg.plugins.len(), cfg.remediators.len());

    let detectors = plugin::load_detectors(&cfg.plugins)?;
    let remediators = plugin::load_remediators(&cfg.remediators)?;

    m.detectors_loaded.set(detectors.len() as i64);
    m.remediators_loaded.set(remediators.len() as i64);
    health.set_loaded(detectors.len(), remediators.len());

    info!(
        detectors = detectors.len(),
        remediators = remediators.len(),
        poll_interval = cfg.daemon.poll_interval_secs,
        metrics_port = cfg.daemon.metrics_port,
        "daemon initialized"
    );

    // Spawn HTTP server (/metrics + /healthz)
    let registry = m.registry.clone();
    let health_clone = health.clone();
    let metrics_port = cfg.daemon.metrics_port;
    tokio::spawn(async move {
        if let Err(e) = metrics::serve_http(metrics_port, registry, health_clone).await {
            error!(error = %e, "http server failed");
        }
    });

    loop {
        info!("running detection cycle");
        let cycle_start = Instant::now();
        m.detection_cycles_total.inc();

        // Phase 1: Detect anomalies from all detector plugins
        let mut all_anomalies = Vec::new();
        for detector in &detectors {
            debug!(detector = detector.name(), "starting detection");
            m.detector_runs_total
                .with_label_values(&[detector.name()])
                .inc();

            match detector
                .detect(&cfg.daemon.lookback, cfg.daemon.frequency_threshold)
                .await
            {
                Ok(anomalies) => {
                    let count = anomalies.len();
                    info!(
                        detector = detector.name(),
                        anomalies = count,
                        "detection complete"
                    );
                    for anomaly in &anomalies {
                        debug!(
                            detector = detector.name(),
                            pattern = %anomaly.pattern,
                            count = anomaly.count,
                            level = ?anomaly.level,
                            labels = ?anomaly.labels,
                            samples = anomaly.samples.len(),
                            "anomaly detail"
                        );
                    }
                    m.anomalies_detected_total
                        .with_label_values(&[detector.name()])
                        .inc_by(count as u64);
                    m.anomalies_per_cycle
                        .with_label_values(&[detector.name()])
                        .set(count as f64);

                    for anomaly in &anomalies {
                        let level = match anomaly.level {
                            detect::LogLevel::Error => "error",
                            detect::LogLevel::Warn => "warn",
                            detect::LogLevel::Unknown => "unknown",
                        };
                        m.anomalies_by_level.with_label_values(&[level]).inc();
                    }

                    all_anomalies.extend(anomalies);
                }
                Err(e) => {
                    error!(detector = detector.name(), error = %e, "detection failed");
                    m.detector_errors_total
                        .with_label_values(&[detector.name()])
                        .inc();
                    m.anomalies_per_cycle
                        .with_label_values(&[detector.name()])
                        .set(0.0);
                }
            }
        }

        // Filter out anomalies matching the deny list
        let before = all_anomalies.len();
        let all_anomalies = filter_denied_anomalies(all_anomalies, &cfg.daemon.deny_labels);
        let denied = before - all_anomalies.len();
        if denied > 0 {
            info!(
                denied = denied,
                remaining = all_anomalies.len(),
                "anomalies suppressed by deny_labels"
            );
        }

        if all_anomalies.is_empty() {
            info!("no anomalies detected this cycle");
        } else {
            info!(
                count = all_anomalies.len(),
                "anomalies detected, proposing remediations"
            );

            // Phase 2: Propose remediations
            for remediator in &remediators {
                debug!(
                    remediator = remediator.name(),
                    anomalies = all_anomalies.len(),
                    "sending anomalies for proposal"
                );
                match remediator.propose(&all_anomalies).await {
                    Ok(actions) => {
                        info!(
                            remediator = remediator.name(),
                            actions = actions.len(),
                            "remediations proposed"
                        );
                        m.remediations_proposed_total
                            .with_label_values(&[remediator.name()])
                            .inc_by(actions.len() as u64);

                        // Track action kinds
                        for action in &actions {
                            let kind_label = match &action.kind {
                                ActionKind::PullRequest { .. } => "pull_request",
                                ActionKind::SshCommand { .. } => "ssh_command",
                                ActionKind::Report { .. } => "report",
                            };
                            m.remediation_actions_by_kind
                                .with_label_values(&[kind_label])
                                .inc();
                        }

                        // Phase 3: Execute proposed actions
                        for action in &actions {
                            debug!(
                                remediator = remediator.name(),
                                description = %action.description,
                                "executing action"
                            );
                            info!(
                                remediator = remediator.name(),
                                action = %action.description,
                                "executing remediation"
                            );
                            let exec_start = Instant::now();
                            match remediator.execute(action).await {
                                Ok(status) => {
                                    let status_label = match &status {
                                        remediate::ActionStatus::Applied => "applied",
                                        remediate::ActionStatus::Proposed => "proposed",
                                        remediate::ActionStatus::Approved => "approved",
                                        remediate::ActionStatus::Failed { .. } => "failed",
                                    };
                                    info!(
                                        remediator = remediator.name(),
                                        status = status_label,
                                        "remediation executed"
                                    );
                                    m.remediations_executed_total
                                        .with_label_values(&[remediator.name(), status_label])
                                        .inc();
                                }
                                Err(e) => {
                                    warn!(
                                        remediator = remediator.name(),
                                        error = %e,
                                        "remediation failed"
                                    );
                                    m.remediation_errors_total
                                        .with_label_values(&[remediator.name()])
                                        .inc();
                                }
                            }
                            m.remediation_duration_seconds
                                .with_label_values(&[remediator.name()])
                                .observe(exec_start.elapsed().as_secs_f64());
                        }
                    }
                    Err(e) => {
                        error!(remediator = remediator.name(), error = %e, "proposal failed");
                        m.remediation_errors_total
                            .with_label_values(&[remediator.name()])
                            .inc();
                    }
                }
            }
        }

        let elapsed = cycle_start.elapsed();
        m.detection_cycle_duration_seconds
            .observe(elapsed.as_secs_f64());
        debug!(elapsed_ms = elapsed.as_millis(), "detection cycle complete");

        debug!(
            sleep_secs = cfg.daemon.poll_interval_secs,
            "sleeping until next cycle"
        );
        tokio::time::sleep(Duration::from_secs(cfg.daemon.poll_interval_secs)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_anomaly(labels: &[(&str, &str)]) -> detect::LogAnomaly {
        detect::LogAnomaly {
            pattern: "test pattern".to_string(),
            count: 1,
            level: detect::LogLevel::Error,
            labels: labels
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
            samples: vec![],
        }
    }

    #[test]
    fn test_empty_deny_list_passes_all() {
        let anomalies = vec![make_anomaly(&[("app", "homeassistant")])];
        let result = filter_denied_anomalies(anomalies, &[]);
        assert_eq!(result.len(), 1);
    }

    #[test]
    fn test_matching_label_is_filtered() {
        let anomalies = vec![
            make_anomaly(&[("app", "homeassistant")]),
            make_anomaly(&[("app", "prometheus")]),
        ];
        let result =
            filter_denied_anomalies(anomalies, &["app=homeassistant".to_string()]);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].labels["app"], "prometheus");
    }

    #[test]
    fn test_multiple_deny_entries_filter_each_match() {
        let anomalies = vec![
            make_anomaly(&[("app", "homeassistant")]),
            make_anomaly(&[("namespace", "legacy")]),
            make_anomaly(&[("app", "prometheus")]),
        ];
        let result = filter_denied_anomalies(
            anomalies,
            &[
                "app=homeassistant".to_string(),
                "namespace=legacy".to_string(),
            ],
        );
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].labels["app"], "prometheus");
    }

    #[test]
    fn test_anomaly_with_no_matching_label_passes() {
        let anomalies = vec![make_anomaly(&[("app", "grafana"), ("env", "prod")])];
        let result =
            filter_denied_anomalies(anomalies, &["app=homeassistant".to_string()]);
        assert_eq!(result.len(), 1);
    }

    #[test]
    fn test_malformed_entry_without_equals_is_skipped() {
        let anomalies = vec![make_anomaly(&[("app", "homeassistant")])];
        let result =
            filter_denied_anomalies(anomalies, &["noequalssign".to_string()]);
        assert_eq!(result.len(), 1);
    }

    #[test]
    fn test_empty_anomalies_list() {
        let result = filter_denied_anomalies(vec![], &["app=homeassistant".to_string()]);
        assert!(result.is_empty());
    }
}
