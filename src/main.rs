mod config;
mod detect;
mod metrics;
mod plugin;
mod remediate;

use std::net::SocketAddr;
use std::time::{Duration, Instant};
use tracing::{error, info, warn};

use crate::remediate::ActionKind;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "logmedic=info".parse().unwrap()),
        )
        .init();

    let config_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "logmedic.toml".to_string());

    info!(config = %config_path, "starting logmedic daemon");
    let cfg = config::load_config(&config_path)?;

    // Initialize metrics
    let m = metrics::Metrics::new()?;
    m.daemon_start_time.set(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
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
    let http_addr: SocketAddr = ([0, 0, 0, 0], cfg.daemon.metrics_port).into();
    let registry = m.registry.clone();
    let health_clone = health.clone();
    tokio::spawn(async move {
        if let Err(e) = metrics::serve_http(http_addr, registry, health_clone).await {
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

        if all_anomalies.is_empty() {
            info!("no anomalies detected this cycle");
        } else {
            info!(count = all_anomalies.len(), "anomalies detected, proposing remediations");

            // Phase 2: Propose remediations
            for remediator in &remediators {
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

        m.detection_cycle_duration_seconds
            .observe(cycle_start.elapsed().as_secs_f64());

        tokio::time::sleep(Duration::from_secs(cfg.daemon.poll_interval_secs)).await;
    }
}
