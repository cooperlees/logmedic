mod native;
#[cfg(feature = "python")]
mod python;

use tracing::{debug, info};

use crate::config::{PluginConfig, PluginKind, RemediatorConfig, RemediatorKind};
use crate::detect::Detector;
use crate::error::PluginError;
use crate::remediate::Remediator;

/// Loads all detector plugins from config.
pub fn load_detectors(configs: &[PluginConfig]) -> Result<Vec<Box<dyn Detector>>, PluginError> {
    let mut detectors: Vec<Box<dyn Detector>> = Vec::new();
    for cfg in configs {
        info!(plugin = %cfg.name, kind = ?cfg.kind, "loading detector plugin");
        debug!(plugin = %cfg.name, path = %cfg.path.display(), settings_keys = ?cfg.settings.keys().collect::<Vec<_>>(), "detector config detail");
        let detector: Box<dyn Detector> = match cfg.kind {
            PluginKind::Native => native::load_native_detector(cfg)?,
            #[cfg(feature = "python")]
            PluginKind::Python => python::load_python_detector(cfg)?,
            #[cfg(not(feature = "python"))]
            PluginKind::Python => {
                return Err(PluginError::PythonNotEnabled {
                    name: cfg.name.clone(),
                })
            }
        };
        debug!(plugin = %cfg.name, "detector plugin loaded successfully");
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
        debug!(remediator = %cfg.name, settings_keys = ?cfg.settings.keys().collect::<Vec<_>>(), "remediator config detail");
        let remediator: Box<dyn Remediator> = match cfg.kind {
            #[cfg(feature = "python")]
            RemediatorKind::Ai => python::load_python_remediator(cfg)?,
            #[cfg(not(feature = "python"))]
            RemediatorKind::Ai => {
                return Err(PluginError::PythonNotEnabled {
                    name: cfg.name.clone(),
                })
            }
            RemediatorKind::Script => native::load_native_remediator(cfg)?,
        };
        debug!(remediator = %cfg.name, "remediator plugin loaded successfully");
        remediators.push(remediator);
    }
    Ok(remediators)
}
