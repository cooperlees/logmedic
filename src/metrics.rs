use std::sync::Arc;

use prometheus::{
    self, Gauge, GaugeVec, Histogram, HistogramOpts, HistogramVec, IntCounter, IntCounterVec,
    IntGauge, Opts, Registry,
};

/// Shared health state checked by the /healthz endpoint.
#[derive(Clone)]
pub struct Health {
    inner: Arc<std::sync::RwLock<HealthState>>,
}

struct HealthState {
    detectors_expected: usize,
    detectors_loaded: usize,
    remediators_expected: usize,
    remediators_loaded: usize,
    ready: bool,
}

#[derive(serde::Serialize)]
struct HealthResponse {
    status: &'static str,
    detectors: PluginHealth,
    remediators: PluginHealth,
}

#[derive(serde::Serialize)]
struct PluginHealth {
    expected: usize,
    loaded: usize,
    ok: bool,
}

impl Health {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(std::sync::RwLock::new(HealthState {
                detectors_expected: 0,
                detectors_loaded: 0,
                remediators_expected: 0,
                remediators_loaded: 0,
                ready: false,
            })),
        }
    }

    pub fn set_expected(&self, detectors: usize, remediators: usize) {
        let mut state = self.inner.write().unwrap();
        state.detectors_expected = detectors;
        state.remediators_expected = remediators;
    }

    pub fn set_loaded(&self, detectors: usize, remediators: usize) {
        let mut state = self.inner.write().unwrap();
        state.detectors_loaded = detectors;
        state.remediators_loaded = remediators;
        state.ready = detectors == state.detectors_expected
            && remediators == state.remediators_expected;
    }

    fn check(&self) -> (bool, String) {
        let state = self.inner.read().unwrap();
        let resp = HealthResponse {
            status: if state.ready { "healthy" } else { "unhealthy" },
            detectors: PluginHealth {
                expected: state.detectors_expected,
                loaded: state.detectors_loaded,
                ok: state.detectors_loaded == state.detectors_expected,
            },
            remediators: PluginHealth {
                expected: state.remediators_expected,
                loaded: state.remediators_loaded,
                ok: state.remediators_loaded == state.remediators_expected,
            },
        };
        (state.ready, serde_json::to_string_pretty(&resp).unwrap())
    }
}

#[derive(Clone)]
pub struct Metrics {
    pub registry: Registry,

    // Plugin inventory
    pub detectors_loaded: IntGauge,
    pub remediators_loaded: IntGauge,

    // Detection cycle
    pub detection_cycles_total: IntCounter,
    pub detection_cycle_duration_seconds: Histogram,

    // Per-detector metrics
    pub detector_runs_total: IntCounterVec,
    pub detector_errors_total: IntCounterVec,
    pub anomalies_detected_total: IntCounterVec,
    pub anomalies_per_cycle: GaugeVec,

    // Per-remediator metrics
    pub remediations_proposed_total: IntCounterVec,
    pub remediations_executed_total: IntCounterVec,
    pub remediation_errors_total: IntCounterVec,
    pub remediation_duration_seconds: HistogramVec,

    // Action kind breakdown
    pub remediation_actions_by_kind: IntCounterVec,

    // Anomaly severity breakdown
    pub anomalies_by_level: IntCounterVec,

    // Daemon uptime
    pub daemon_start_time: Gauge,
}

impl Metrics {
    pub fn new() -> anyhow::Result<Self> {
        let registry = Registry::new();

        let detectors_loaded = IntGauge::new(
            "logmedic_detectors_loaded",
            "Number of detector plugins currently loaded",
        )?;
        let remediators_loaded = IntGauge::new(
            "logmedic_remediators_loaded",
            "Number of remediator plugins currently loaded",
        )?;

        let detection_cycles_total = IntCounter::new(
            "logmedic_detection_cycles_total",
            "Total number of detection cycles run",
        )?;
        let detection_cycle_duration_seconds = Histogram::with_opts(HistogramOpts::new(
            "logmedic_detection_cycle_duration_seconds",
            "Duration of a full detection cycle in seconds",
        ))?;

        let detector_runs_total = IntCounterVec::new(
            Opts::new(
                "logmedic_detector_runs_total",
                "Total runs per detector plugin",
            ),
            &["detector"],
        )?;
        let detector_errors_total = IntCounterVec::new(
            Opts::new(
                "logmedic_detector_errors_total",
                "Total errors per detector plugin",
            ),
            &["detector"],
        )?;
        let anomalies_detected_total = IntCounterVec::new(
            Opts::new(
                "logmedic_anomalies_detected_total",
                "Total anomalies detected per detector",
            ),
            &["detector"],
        )?;
        let anomalies_per_cycle = GaugeVec::new(
            Opts::new(
                "logmedic_anomalies_per_cycle",
                "Number of anomalies found in the most recent cycle per detector",
            ),
            &["detector"],
        )?;

        let remediations_proposed_total = IntCounterVec::new(
            Opts::new(
                "logmedic_remediations_proposed_total",
                "Total remediation actions proposed per remediator",
            ),
            &["remediator"],
        )?;
        let remediations_executed_total = IntCounterVec::new(
            Opts::new(
                "logmedic_remediations_executed_total",
                "Total remediation actions executed per remediator and status",
            ),
            &["remediator", "status"],
        )?;
        let remediation_errors_total = IntCounterVec::new(
            Opts::new(
                "logmedic_remediation_errors_total",
                "Total remediation errors per remediator",
            ),
            &["remediator"],
        )?;
        let remediation_duration_seconds = HistogramVec::new(
            HistogramOpts::new(
                "logmedic_remediation_duration_seconds",
                "Duration of remediation execution in seconds",
            ),
            &["remediator"],
        )?;

        let remediation_actions_by_kind = IntCounterVec::new(
            Opts::new(
                "logmedic_remediation_actions_by_kind_total",
                "Total remediation actions broken down by kind",
            ),
            &["kind"],
        )?;

        let anomalies_by_level = IntCounterVec::new(
            Opts::new(
                "logmedic_anomalies_by_level_total",
                "Total anomalies broken down by log level",
            ),
            &["level"],
        )?;

        let daemon_start_time = Gauge::new(
            "logmedic_daemon_start_time_seconds",
            "Unix timestamp when the daemon started",
        )?;

        // Register all metrics
        registry.register(Box::new(detectors_loaded.clone()))?;
        registry.register(Box::new(remediators_loaded.clone()))?;
        registry.register(Box::new(detection_cycles_total.clone()))?;
        registry.register(Box::new(detection_cycle_duration_seconds.clone()))?;
        registry.register(Box::new(detector_runs_total.clone()))?;
        registry.register(Box::new(detector_errors_total.clone()))?;
        registry.register(Box::new(anomalies_detected_total.clone()))?;
        registry.register(Box::new(anomalies_per_cycle.clone()))?;
        registry.register(Box::new(remediations_proposed_total.clone()))?;
        registry.register(Box::new(remediations_executed_total.clone()))?;
        registry.register(Box::new(remediation_errors_total.clone()))?;
        registry.register(Box::new(remediation_duration_seconds.clone()))?;
        registry.register(Box::new(remediation_actions_by_kind.clone()))?;
        registry.register(Box::new(anomalies_by_level.clone()))?;
        registry.register(Box::new(daemon_start_time.clone()))?;

        Ok(Self {
            registry,
            detectors_loaded,
            remediators_loaded,
            detection_cycles_total,
            detection_cycle_duration_seconds,
            detector_runs_total,
            detector_errors_total,
            anomalies_detected_total,
            anomalies_per_cycle,
            remediations_proposed_total,
            remediations_executed_total,
            remediation_errors_total,
            remediation_duration_seconds,
            remediation_actions_by_kind,
            anomalies_by_level,
            daemon_start_time,
        })
    }
}

/// Spawn an HTTP server that serves /metrics and /healthz.
pub async fn serve_http(
    addr: std::net::SocketAddr,
    registry: Registry,
    health: Health,
) -> anyhow::Result<()> {
    use http_body_util::Full;
    use hyper::body::Bytes;
    use hyper::service::service_fn;
    use hyper::{Request, Response};
    use hyper_util::rt::TokioIo;
    use prometheus::Encoder;
    use tokio::net::TcpListener;

    let listener = TcpListener::bind(addr).await?;
    tracing::info!(addr = %addr, "http server listening (/metrics, /healthz)");

    loop {
        let (stream, _) = listener.accept().await?;
        let io = TokioIo::new(stream);
        let registry = registry.clone();
        let health = health.clone();

        tokio::spawn(async move {
            let service = service_fn(move |req: Request<hyper::body::Incoming>| {
                let registry = registry.clone();
                let health = health.clone();
                async move {
                    match req.uri().path() {
                        "/metrics" => {
                            let encoder = prometheus::TextEncoder::new();
                            let metric_families = registry.gather();
                            let mut buffer = Vec::new();
                            encoder.encode(&metric_families, &mut buffer).unwrap();
                            Ok::<_, hyper::Error>(
                                Response::builder()
                                    .header("Content-Type", encoder.format_type())
                                    .body(Full::new(Bytes::from(buffer)))
                                    .unwrap(),
                            )
                        }
                        "/healthz" => {
                            let (healthy, body) = health.check();
                            let status = if healthy { 200 } else { 503 };
                            Ok(Response::builder()
                                .status(status)
                                .header("Content-Type", "application/json")
                                .body(Full::new(Bytes::from(body)))
                                .unwrap())
                        }
                        _ => Ok(Response::builder()
                            .status(404)
                            .body(Full::new(Bytes::from("Not Found")))
                            .unwrap()),
                    }
                }
            });

            if let Err(e) = hyper::server::conn::http1::Builder::new()
                .serve_connection(io, service)
                .await
            {
                tracing::warn!(error = %e, "http connection error");
            }
        });
    }
}
