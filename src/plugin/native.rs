use anyhow::{Context, Result};
use libloading::{Library, Symbol};

use crate::config::{PluginConfig, RemediatorConfig};
use crate::detect::Detector;
use crate::remediate::Remediator;

/// Native detector plugins are shared libraries (.so/.dylib) that export:
///   fn create_detector(settings: &str) -> Box<dyn Detector>
type CreateDetectorFn = unsafe fn(settings: &str) -> Box<dyn Detector>;

/// Native remediator plugins export:
///   fn create_remediator(settings: &str) -> Box<dyn Remediator>
type CreateRemediatorFn = unsafe fn(settings: &str) -> Box<dyn Remediator>;

pub fn load_native_detector(cfg: &PluginConfig) -> Result<Box<dyn Detector>> {
    let settings_json = serde_json::to_string(&cfg.settings)?;
    unsafe {
        let lib = Library::new(&cfg.path)
            .with_context(|| format!("failed to load native plugin: {}", cfg.path.display()))?;
        let create: Symbol<CreateDetectorFn> = lib
            .get(b"create_detector")
            .context("plugin missing `create_detector` symbol")?;
        let detector = create(&settings_json);
        // Leak the library so it stays loaded for the process lifetime
        std::mem::forget(lib);
        Ok(detector)
    }
}

pub fn load_native_remediator(cfg: &RemediatorConfig) -> Result<Box<dyn Remediator>> {
    let settings_json = serde_json::to_string(&cfg.settings)?;
    let path = cfg
        .settings
        .get("path")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    unsafe {
        let lib = Library::new(path)
            .with_context(|| format!("failed to load native remediator: {path}"))?;
        let create: Symbol<CreateRemediatorFn> = lib
            .get(b"create_remediator")
            .context("plugin missing `create_remediator` symbol")?;
        let remediator = create(&settings_json);
        std::mem::forget(lib);
        Ok(remediator)
    }
}
