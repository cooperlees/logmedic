use async_trait::async_trait;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::config::{PluginConfig, RemediatorConfig};
use crate::detect::{Detector, LogAnomaly, LogLevel};
use crate::error::PluginError;
use crate::remediate::{ActionStatus, RemediationAction, Remediator};

// ── Helpers ─────────────────────────────────────────────────────────

fn module_dir(path: &str) -> String {
    std::path::Path::new(path)
        .parent()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|| ".".to_string())
}

fn module_stem(path: &str) -> String {
    std::path::Path::new(path)
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| path.to_string())
}

fn setup_sys_path<'py>(py: Python<'py>, plugin_name: &str, plugin_path: &str) -> Result<(), PluginError> {
    let sys = py.import_bound("sys").map_err(|e| PluginError::PythonSysPathError {
        name: plugin_name.to_string(),
        detail: e.to_string(),
    })?;
    let path: Bound<'py, pyo3::types::PyList> = sys
        .getattr("path")
        .map_err(|e| PluginError::PythonSysPathError {
            name: plugin_name.to_string(),
            detail: e.to_string(),
        })?
        .downcast_into()
        .map_err(|e| PluginError::PythonSysPathError {
            name: plugin_name.to_string(),
            detail: e.to_string(),
        })?;
    path.insert(0, module_dir(plugin_path)).map_err(|e| PluginError::PythonSysPathError {
        name: plugin_name.to_string(),
        detail: e.to_string(),
    })?;
    Ok(())
}

// ── PythonDetector ──────────────────────────────────────────────────

/// A detector backed by a Python module.
/// The Python module must define a class `DetectorPlugin` with:
///   - `__init__(self, settings: dict)`
///   - `detect(self, lookback: str, threshold: int) -> list[dict]`
struct PythonDetector {
    plugin_name: String,
    module_path: String,
    settings_json: String,
}

impl PythonDetector {
    fn call_detect(&self, lookback: &str, threshold: u64) -> Result<Vec<LogAnomaly>, PluginError> {
        Python::with_gil(|py| {
            setup_sys_path(py, &self.plugin_name, &self.module_path)?;

            let stem = module_stem(&self.module_path);
            let module = py.import_bound(stem.as_str()).map_err(|e| PluginError::PythonImportFailed {
                name: self.plugin_name.clone(),
                path: self.module_path.clone(),
                module: stem.clone(),
                detail: format_python_error(py, &e),
            })?;

            let settings: Bound<'_, PyDict> = PyDict::new_bound(py);
            settings
                .set_item("settings_json", &self.settings_json)
                .map_err(|e| PluginError::PythonMethodCallFailed {
                    name: self.plugin_name.clone(),
                    class: "DetectorPlugin".to_string(),
                    method: "__init__".to_string(),
                    expected_signature: "__init__(self, settings: dict)".to_string(),
                    detail: e.to_string(),
                })?;

            let plugin =
                module
                    .call_method1("DetectorPlugin", (&settings,))
                    .map_err(|e| PluginError::PythonMissingClass {
                        name: self.plugin_name.clone(),
                        module: stem.clone(),
                        class: "DetectorPlugin".to_string(),
                        detail: format_python_error(py, &e),
                    })?;

            let result =
                plugin
                    .call_method1("detect", (lookback, threshold))
                    .map_err(|e| PluginError::PythonMethodCallFailed {
                        name: self.plugin_name.clone(),
                        class: "DetectorPlugin".to_string(),
                        method: "detect".to_string(),
                        expected_signature: "detect(self, lookback: str, threshold: int) -> list[dict]".to_string(),
                        detail: format_python_error(py, &e),
                    })?;

            let dicts: Vec<Bound<'_, PyDict>> =
                result
                    .extract()
                    .map_err(|e| PluginError::PythonReturnTypeError {
                        name: self.plugin_name.clone(),
                        method: "detect".to_string(),
                        expected_type: "list[dict] where each dict has 'pattern' (str) and 'count' (int)".to_string(),
                        detail: e.to_string(),
                    })?;

            let mut anomalies = Vec::with_capacity(dicts.len());
            for (i, d) in dicts.iter().enumerate() {
                anomalies.push(parse_anomaly_dict(&self.plugin_name, i, d)?);
            }
            Ok(anomalies)
        })
    }
}

#[async_trait]
impl Detector for PythonDetector {
    fn name(&self) -> &str {
        &self.plugin_name
    }

    async fn detect(&self, lookback: &str, threshold: u64) -> Result<Vec<LogAnomaly>, PluginError> {
        let lookback = lookback.to_string();
        let detector = PythonDetector {
            plugin_name: self.plugin_name.clone(),
            module_path: self.module_path.clone(),
            settings_json: self.settings_json.clone(),
        };
        let name = self.plugin_name.clone();
        tokio::task::spawn_blocking(move || detector.call_detect(&lookback, threshold))
            .await
            .map_err(|e| PluginError::TaskJoinError {
                name,
                source: e,
            })?
    }
}

fn parse_anomaly_dict(
    plugin_name: &str,
    index: usize,
    d: &Bound<'_, PyDict>,
) -> Result<LogAnomaly, PluginError> {
    let pattern: String = d
        .get_item("pattern")
        .ok()
        .flatten()
        .ok_or_else(|| PluginError::PythonAnomalyMissingKey {
            name: plugin_name.to_string(),
            index,
            key: "pattern".to_string(),
        })?
        .extract()
        .map_err(|_| PluginError::PythonAnomalyMissingKey {
            name: plugin_name.to_string(),
            index,
            key: "pattern".to_string(),
        })?;

    let count: u64 = d
        .get_item("count")
        .ok()
        .flatten()
        .ok_or_else(|| PluginError::PythonAnomalyMissingKey {
            name: plugin_name.to_string(),
            index,
            key: "count".to_string(),
        })?
        .extract()
        .map_err(|_| PluginError::PythonAnomalyMissingKey {
            name: plugin_name.to_string(),
            index,
            key: "count".to_string(),
        })?;

    let level_str: String = d
        .get_item("level")
        .ok()
        .flatten()
        .and_then(|v| v.extract().ok())
        .unwrap_or_default();
    let level = match level_str.to_lowercase().as_str() {
        "error" => LogLevel::Error,
        "warn" | "warning" => LogLevel::Warn,
        _ => LogLevel::Unknown,
    };

    let labels: std::collections::HashMap<String, String> = d
        .get_item("labels")
        .ok()
        .flatten()
        .and_then(|v| v.extract().ok())
        .unwrap_or_default();

    let samples: Vec<String> = d
        .get_item("samples")
        .ok()
        .flatten()
        .and_then(|v| v.extract().ok())
        .unwrap_or_default();

    Ok(LogAnomaly {
        pattern,
        count,
        level,
        labels,
        samples,
    })
}

pub fn load_python_detector(cfg: &PluginConfig) -> Result<Box<dyn Detector>, PluginError> {
    let settings_json =
        serde_json::to_string(&cfg.settings).map_err(|e| PluginError::SettingsSerializationFailed {
            name: cfg.name.clone(),
            source: e,
        })?;
    Ok(Box::new(PythonDetector {
        plugin_name: cfg.name.clone(),
        module_path: cfg.path.to_string_lossy().to_string(),
        settings_json,
    }))
}

// ── PythonRemediator ────────────────────────────────────────────────

/// A remediator backed by a Python module.
/// The Python module must define a class `RemediatorPlugin` with:
///   - `__init__(self, settings: dict)`
///   - `propose(self, anomalies_json: str) -> str` (JSON array of actions)
///   - `execute(self, action_json: str) -> str` (JSON status)
struct PythonRemediator {
    plugin_name: String,
    module_path: String,
    settings_json: String,
}

impl PythonRemediator {
    fn call_propose(&self, anomalies_json: &str) -> Result<Vec<RemediationAction>, PluginError> {
        Python::with_gil(|py| {
            let plugin = self.instantiate(py, "RemediatorPlugin")?;
            let result = plugin
                .call_method1("propose", (anomalies_json,))
                .map_err(|e| PluginError::PythonMethodCallFailed {
                    name: self.plugin_name.clone(),
                    class: "RemediatorPlugin".to_string(),
                    method: "propose".to_string(),
                    expected_signature: "propose(self, anomalies_json: str) -> str".to_string(),
                    detail: format_python_error(py, &e),
                })?;

            let actions_json: String =
                result
                    .extract()
                    .map_err(|e| PluginError::PythonReturnTypeError {
                        name: self.plugin_name.clone(),
                        method: "propose".to_string(),
                        expected_type: "str (JSON array of remediation actions)".to_string(),
                        detail: e.to_string(),
                    })?;

            let actions: Vec<RemediationAction> =
                serde_json::from_str(&actions_json).map_err(|e| PluginError::PythonJsonParseError {
                    name: self.plugin_name.clone(),
                    source: e,
                })?;
            Ok(actions)
        })
    }

    fn call_execute(&self, action_json: &str) -> Result<ActionStatus, PluginError> {
        Python::with_gil(|py| {
            let plugin = self.instantiate(py, "RemediatorPlugin")?;
            let result = plugin
                .call_method1("execute", (action_json,))
                .map_err(|e| PluginError::PythonMethodCallFailed {
                    name: self.plugin_name.clone(),
                    class: "RemediatorPlugin".to_string(),
                    method: "execute".to_string(),
                    expected_signature: "execute(self, action_json: str) -> str".to_string(),
                    detail: format_python_error(py, &e),
                })?;

            let status_json: String =
                result
                    .extract()
                    .map_err(|e| PluginError::PythonReturnTypeError {
                        name: self.plugin_name.clone(),
                        method: "execute".to_string(),
                        expected_type: "str (JSON status object)".to_string(),
                        detail: e.to_string(),
                    })?;

            let status: ActionStatus =
                serde_json::from_str(&status_json).map_err(|e| PluginError::PythonJsonParseError {
                    name: self.plugin_name.clone(),
                    source: e,
                })?;
            Ok(status)
        })
    }

    fn instantiate<'py>(
        &self,
        py: Python<'py>,
        class_name: &str,
    ) -> Result<Bound<'py, PyAny>, PluginError> {
        setup_sys_path(py, &self.plugin_name, &self.module_path)?;

        let stem = module_stem(&self.module_path);
        let module = py.import_bound(stem.as_str()).map_err(|e| PluginError::PythonImportFailed {
            name: self.plugin_name.clone(),
            path: self.module_path.clone(),
            module: stem.clone(),
            detail: format_python_error(py, &e),
        })?;

        let settings: Bound<'_, PyDict> = PyDict::new_bound(py);
        settings
            .set_item("settings_json", &self.settings_json)
            .map_err(|e| PluginError::PythonMethodCallFailed {
                name: self.plugin_name.clone(),
                class: class_name.to_string(),
                method: "__init__".to_string(),
                expected_signature: "__init__(self, settings: dict)".to_string(),
                detail: e.to_string(),
            })?;

        module
            .call_method1(class_name, (&settings,))
            .map_err(|e| PluginError::PythonMissingClass {
                name: self.plugin_name.clone(),
                module: stem,
                class: class_name.to_string(),
                detail: format_python_error(py, &e),
            })
    }
}

#[async_trait]
impl Remediator for PythonRemediator {
    fn name(&self) -> &str {
        &self.plugin_name
    }

    async fn propose(
        &self,
        anomalies: &[LogAnomaly],
    ) -> Result<Vec<RemediationAction>, PluginError> {
        let anomalies_json = serde_json::to_string(anomalies).map_err(|e| {
            PluginError::SettingsSerializationFailed {
                name: self.plugin_name.clone(),
                source: e,
            }
        })?;
        let remediator = PythonRemediator {
            plugin_name: self.plugin_name.clone(),
            module_path: self.module_path.clone(),
            settings_json: self.settings_json.clone(),
        };
        let name = self.plugin_name.clone();
        tokio::task::spawn_blocking(move || remediator.call_propose(&anomalies_json))
            .await
            .map_err(|e| PluginError::TaskJoinError {
                name,
                source: e,
            })?
    }

    async fn execute(&self, action: &RemediationAction) -> Result<ActionStatus, PluginError> {
        let action_json = serde_json::to_string(action).map_err(|e| {
            PluginError::SettingsSerializationFailed {
                name: self.plugin_name.clone(),
                source: e,
            }
        })?;
        let remediator = PythonRemediator {
            plugin_name: self.plugin_name.clone(),
            module_path: self.module_path.clone(),
            settings_json: self.settings_json.clone(),
        };
        let name = self.plugin_name.clone();
        tokio::task::spawn_blocking(move || remediator.call_execute(&action_json))
            .await
            .map_err(|e| PluginError::TaskJoinError {
                name,
                source: e,
            })?
    }
}

pub fn load_python_remediator(cfg: &RemediatorConfig) -> Result<Box<dyn Remediator>, PluginError> {
    let settings_json =
        serde_json::to_string(&cfg.settings).map_err(|e| PluginError::SettingsSerializationFailed {
            name: cfg.name.clone(),
            source: e,
        })?;
    let module_path = cfg
        .settings
        .get("path")
        .and_then(|v| v.as_str())
        .ok_or_else(|| PluginError::MissingPluginPath {
            name: cfg.name.clone(),
        })?
        .to_string();
    Ok(Box::new(PythonRemediator {
        plugin_name: cfg.name.clone(),
        settings_json,
        module_path,
    }))
}

/// Format a PyErr with its full traceback for maximum debuggability.
fn format_python_error(py: Python<'_>, err: &PyErr) -> String {
    // Try to get the full traceback
    if let Some(tb) = err.traceback_bound(py).as_ref() {
        if let Ok(formatted) = tb.format() {
            return format!("{formatted}{err}");
        }
    }
    err.to_string()
}
