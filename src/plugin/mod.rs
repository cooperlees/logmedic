mod native;
mod python;

use tracing::info;

use crate::config::{PluginConfig, PluginKind, RemediatorConfig, RemediatorKind};
use crate::detect::Detector;
use crate::error::PluginError;
use crate::remediate::Remediator;

/// Loads all detector plugins from config.
pub fn load_detectors(configs: &[PluginConfig]) -> Result<Vec<Box<dyn Detector>>, PluginError> {
    let mut detectors: Vec<Box<dyn Detector>> = Vec::new();
    for cfg in configs {
        info!(plugin = %cfg.name, kind = ?cfg.kind, "loading detector plugin");
        let detector: Box<dyn Detector> = match cfg.kind {
            PluginKind::Native => native::load_native_detector(cfg)?,
            PluginKind::Python => python::load_python_detector(cfg)?,
        };
        detectors.push(detector);
    }
    Ok(detectors)
}

/// Loads all remediator plugins from config.
pub fn load_remediators(
    configs: &[RemediatorConfig],
) -> Result<Vec<Box<dyn Remediator>>, PluginError> {
    let mut remediators: Vec<Box<dyn Remediator>> = Vec::new();
    for cfg in configs {
        info!(remediator = %cfg.name, kind = ?cfg.kind, "loading remediator");
        let remediator: Box<dyn Remediator> = match cfg.kind {
            RemediatorKind::Ai => python::load_python_remediator(cfg)?,
            RemediatorKind::Script => native::load_native_remediator(cfg)?,
        };
        remediators.push(remediator);
    }
    Ok(remediators)
}
