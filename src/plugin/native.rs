use libloading::{Library, Symbol};

use crate::config::{PluginConfig, RemediatorConfig};
use crate::detect::Detector;
use crate::error::PluginError;
use crate::remediate::Remediator;

/// Native detector plugins are shared libraries (.so/.dylib) that export:
///   fn create_detector(settings: &str) -> Box<dyn Detector>
type CreateDetectorFn = unsafe fn(settings: &str) -> Box<dyn Detector>;

/// Native remediator plugins export:
///   fn create_remediator(settings: &str) -> Box<dyn Remediator>
type CreateRemediatorFn = unsafe fn(settings: &str) -> Box<dyn Remediator>;

pub fn load_native_detector(cfg: &PluginConfig) -> Result<Box<dyn Detector>, PluginError> {
    let settings_json =
        serde_json::to_string(&cfg.settings).map_err(|e| PluginError::SettingsSerializationFailed {
            name: cfg.name.clone(),
            source: e,
        })?;
    unsafe {
        let lib = Library::new(&cfg.path).map_err(|e| PluginError::NativeLoadFailed {
            name: cfg.name.clone(),
            path: cfg.path.clone(),
            source: e,
        })?;
        let create: Symbol<CreateDetectorFn> =
            lib.get(b"create_detector")
                .map_err(|e| PluginError::NativeMissingSymbol {
                    name: cfg.name.clone(),
                    path: cfg.path.clone(),
                    symbol: "create_detector".to_string(),
                    source: e,
                })?;
        let detector = create(&settings_json);
        // Leak the library so it stays loaded for the process lifetime
        std::mem::forget(lib);
        Ok(detector)
    }
}

pub fn load_native_remediator(cfg: &RemediatorConfig) -> Result<Box<dyn Remediator>, PluginError> {
    let settings_json =
        serde_json::to_string(&cfg.settings).map_err(|e| PluginError::SettingsSerializationFailed {
            name: cfg.name.clone(),
            source: e,
        })?;
    let path = cfg
        .settings
        .get("path")
        .and_then(|v| v.as_str())
        .ok_or_else(|| PluginError::MissingPluginPath {
            name: cfg.name.clone(),
        })?;
    unsafe {
        let lib = Library::new(path).map_err(|e| PluginError::NativeLoadFailed {
            name: cfg.name.clone(),
            path: path.into(),
            source: e,
        })?;
        let create: Symbol<CreateRemediatorFn> =
            lib.get(b"create_remediator")
                .map_err(|e| PluginError::NativeMissingSymbol {
                    name: cfg.name.clone(),
                    path: path.into(),
                    symbol: "create_remediator".to_string(),
                    source: e,
                })?;
        let remediator = create(&settings_json);
        std::mem::forget(lib);
        Ok(remediator)
    }
}
