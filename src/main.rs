mod config;
mod detect;
mod plugin;
mod remediate;

use std::time::Duration;
use tracing::{error, info, warn};

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

    let detectors = plugin::load_detectors(&cfg.plugins)?;
    let remediators = plugin::load_remediators(&cfg.remediators)?;

    info!(
        detectors = detectors.len(),
        remediators = remediators.len(),
        poll_interval = cfg.daemon.poll_interval_secs,
        "daemon initialized"
    );

    loop {
        info!("running detection cycle");

        // Phase 1: Detect anomalies from all detector plugins
        let mut all_anomalies = Vec::new();
        for detector in &detectors {
            match detector
                .detect(&cfg.daemon.lookback, cfg.daemon.frequency_threshold)
                .await
            {
                Ok(anomalies) => {
                    info!(
                        detector = detector.name(),
                        anomalies = anomalies.len(),
                        "detection complete"
                    );
                    all_anomalies.extend(anomalies);
                }
                Err(e) => {
                    error!(detector = detector.name(), error = %e, "detection failed");
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

                        // Phase 3: Execute proposed actions
                        for action in &actions {
                            info!(
                                remediator = remediator.name(),
                                action = %action.description,
                                "executing remediation"
                            );
                            match remediator.execute(action).await {
                                Ok(status) => {
                                    info!(
                                        remediator = remediator.name(),
                                        status = ?status,
                                        "remediation executed"
                                    );
                                }
                                Err(e) => {
                                    warn!(
                                        remediator = remediator.name(),
                                        error = %e,
                                        "remediation failed"
                                    );
                                }
                            }
                        }
                    }
                    Err(e) => {
                        error!(remediator = remediator.name(), error = %e, "proposal failed");
                    }
                }
            }
        }

        tokio::time::sleep(Duration::from_secs(cfg.daemon.poll_interval_secs)).await;
    }
}
