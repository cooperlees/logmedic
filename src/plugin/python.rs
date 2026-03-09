use anyhow::{Context, Result};
use async_trait::async_trait;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::config::{PluginConfig, RemediatorConfig};
use crate::detect::{Detector, LogAnomaly, LogLevel};
use crate::remediate::{ActionStatus, RemediationAction, Remediator};

/// A detector backed by a Python module.
/// The Python module must define a class `DetectorPlugin` with:
///   - `name(self) -> str`
///   - `detect(self, lookback: str, threshold: int) -> list[dict]`
struct PythonDetector {
    module_name: String,
    settings_json: String,
}

impl PythonDetector {
    fn call_detect(&self, lookback: &str, threshold: u64) -> Result<Vec<LogAnomaly>> {
        Python::with_gil(|py| {
            let sys = py.import_bound("sys")?;
            let path: Bound<'_, pyo3::types::PyList> = sys.getattr("path")?.downcast_into().map_err(|e| anyhow::anyhow!("{e}"))?;

            // Add plugin directory to Python path if not already there
            let module_dir = std::path::Path::new(&self.module_name)
                .parent()
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_else(|| ".".to_string());
            path.insert(0, &module_dir)?;

            let module_stem = std::path::Path::new(&self.module_name)
                .file_stem()
                .map(|s| s.to_string_lossy().to_string())
                .unwrap_or_else(|| self.module_name.clone());

            let module = py.import_bound(module_stem.as_str())?;
            let settings: Bound<'_, PyDict> = PyDict::new_bound(py);
            settings.set_item("settings_json", &self.settings_json)?;

            let plugin = module.call_method1("DetectorPlugin", (&settings,))?;
            let result = plugin.call_method1("detect", (lookback, threshold))?;

            let anomalies: Vec<LogAnomaly> = result
                .extract::<Vec<Bound<'_, PyDict>>>()?
                .iter()
                .filter_map(|d| parse_anomaly_dict(d).ok())
                .collect();

            Ok(anomalies)
        })
    }
}

#[async_trait]
impl Detector for PythonDetector {
    fn name(&self) -> &str {
        &self.module_name
    }

    async fn detect(&self, lookback: &str, threshold: u64) -> Result<Vec<LogAnomaly>> {
        let lookback = lookback.to_string();
        let module_name = self.module_name.clone();
        let settings_json = self.settings_json.clone();

        // Run Python in a blocking task to avoid blocking the async runtime
        let detector = PythonDetector {
            module_name,
            settings_json,
        };
        tokio::task::spawn_blocking(move || detector.call_detect(&lookback, threshold)).await?
    }
}

fn parse_anomaly_dict(d: &Bound<'_, PyDict>) -> Result<LogAnomaly> {
    let pattern: String = d
        .get_item("pattern")?
        .context("missing 'pattern'")?
        .extract()?;
    let count: u64 = d
        .get_item("count")?
        .context("missing 'count'")?
        .extract()?;
    let level_str: String = d
        .get_item("level")?
        .map(|v| v.extract().unwrap_or_default())
        .unwrap_or_default();
    let level = match level_str.to_lowercase().as_str() {
        "error" => LogLevel::Error,
        "warn" | "warning" => LogLevel::Warn,
        _ => LogLevel::Unknown,
    };
    let labels: std::collections::HashMap<String, String> = d
        .get_item("labels")?
        .map(|v| v.extract().unwrap_or_default())
        .unwrap_or_default();
    let samples: Vec<String> = d
        .get_item("samples")?
        .map(|v| v.extract().unwrap_or_default())
        .unwrap_or_default();

    Ok(LogAnomaly {
        pattern,
        count,
        level,
        labels,
        samples,
    })
}

pub fn load_python_detector(cfg: &PluginConfig) -> Result<Box<dyn Detector>> {
    let settings_json = serde_json::to_string(&cfg.settings)?;
    Ok(Box::new(PythonDetector {
        module_name: cfg.path.to_string_lossy().to_string(),
        settings_json,
    }))
}

/// A remediator backed by a Python module.
/// The Python module must define a class `RemediatorPlugin` with:
///   - `name(self) -> str`
///   - `propose(self, anomalies_json: str) -> list[dict]`
///   - `execute(self, action_json: str) -> dict`
struct PythonRemediator {
    name: String,
    settings_json: String,
    module_path: String,
}

#[async_trait]
impl Remediator for PythonRemediator {
    fn name(&self) -> &str {
        &self.name
    }

    async fn propose(&self, anomalies: &[LogAnomaly]) -> Result<Vec<RemediationAction>> {
        let anomalies_json = serde_json::to_string(anomalies)?;
        let module_path = self.module_path.clone();
        let settings_json = self.settings_json.clone();

        tokio::task::spawn_blocking(move || {
            Python::with_gil(|py| {
                let sys = py.import_bound("sys")?;
                let path: Bound<'_, pyo3::types::PyList> = sys.getattr("path")?.downcast_into().map_err(|e| anyhow::anyhow!("{e}"))?;

                let module_dir = std::path::Path::new(&module_path)
                    .parent()
                    .map(|p| p.to_string_lossy().to_string())
                    .unwrap_or_else(|| ".".to_string());
                path.insert(0, &module_dir)?;

                let module_stem = std::path::Path::new(&module_path)
                    .file_stem()
                    .map(|s| s.to_string_lossy().to_string())
                    .unwrap_or_else(|| module_path.clone());

                let module = py.import_bound(module_stem.as_str())?;
                let settings: Bound<'_, PyDict> = PyDict::new_bound(py);
                settings.set_item("settings_json", &settings_json)?;

                let plugin = module.call_method1("RemediatorPlugin", (&settings,))?;
                let result = plugin.call_method1("propose", (&anomalies_json,))?;

                let actions_json: String = result.extract()?;
                let actions: Vec<RemediationAction> = serde_json::from_str(&actions_json)?;
                Ok(actions)
            })
        })
        .await?
    }

    async fn execute(&self, action: &RemediationAction) -> Result<ActionStatus> {
        let action_json = serde_json::to_string(action)?;
        let module_path = self.module_path.clone();
        let settings_json = self.settings_json.clone();

        tokio::task::spawn_blocking(move || {
            Python::with_gil(|py| {
                let sys = py.import_bound("sys")?;
                let path: Bound<'_, pyo3::types::PyList> = sys.getattr("path")?.downcast_into().map_err(|e| anyhow::anyhow!("{e}"))?;

                let module_dir = std::path::Path::new(&module_path)
                    .parent()
                    .map(|p| p.to_string_lossy().to_string())
                    .unwrap_or_else(|| ".".to_string());
                path.insert(0, &module_dir)?;

                let module_stem = std::path::Path::new(&module_path)
                    .file_stem()
                    .map(|s| s.to_string_lossy().to_string())
                    .unwrap_or_else(|| module_path.clone());

                let module = py.import_bound(module_stem.as_str())?;
                let settings: Bound<'_, PyDict> = PyDict::new_bound(py);
                settings.set_item("settings_json", &settings_json)?;

                let plugin = module.call_method1("RemediatorPlugin", (&settings,))?;
                let result = plugin.call_method1("execute", (&action_json,))?;

                let status_json: String = result.extract()?;
                let status: ActionStatus = serde_json::from_str(&status_json)?;
                Ok(status)
            })
        })
        .await?
    }
}

pub fn load_python_remediator(cfg: &RemediatorConfig) -> Result<Box<dyn Remediator>> {
    let settings_json = serde_json::to_string(&cfg.settings)?;
    let module_path = cfg
        .settings
        .get("path")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    Ok(Box::new(PythonRemediator {
        name: cfg.name.clone(),
        settings_json,
        module_path,
    }))
}
