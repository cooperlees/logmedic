use std::path::PathBuf;

/// Top-level daemon errors.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("failed to load config from '{path}': {source}")]
    ConfigLoad {
        path: String,
        source: Box<dyn std::error::Error + Send + Sync>,
    },

    #[error("failed to parse config '{path}': {source}")]
    ConfigParse {
        path: String,
        source: toml::de::Error,
    },

    #[error(transparent)]
    Plugin(#[from] PluginError),

    #[error("metrics initialization failed: {0}")]
    Metrics(#[from] prometheus::Error),

    #[error("http server failed on {addr}: {source}")]
    #[allow(dead_code)]
    HttpServer {
        addr: std::net::SocketAddr,
        source: std::io::Error,
    },
}

/// Errors specific to plugin loading and execution.
/// Designed to give plugin authors maximum context for debugging.
#[derive(Debug, thiserror::Error)]
pub enum PluginError {
    // ── Native plugin errors ────────────────────────────────────────
    #[error(
        "failed to load native plugin '{name}' from '{path}': {source}\n\
         hint: ensure the shared library exists and is compiled for this platform"
    )]
    NativeLoadFailed {
        name: String,
        path: PathBuf,
        source: libloading::Error,
    },

    #[error(
        "native plugin '{name}' at '{path}' is missing the expected symbol '{symbol}'\n\
         hint: your plugin must export `{symbol}` as a public extern fn"
    )]
    NativeMissingSymbol {
        name: String,
        path: PathBuf,
        symbol: String,
        source: libloading::Error,
    },

    // ── Python plugin errors ────────────────────────────────────────
    #[cfg(feature = "python")]
    #[error(
        "python plugin '{name}': failed to import module '{module}'\n\
         hint: check that '{path}' exists and has no syntax errors. \
         Try running `python -c \"import {module}\"` to verify.\n\
         python error: {detail}"
    )]
    PythonImportFailed {
        name: String,
        path: String,
        module: String,
        detail: String,
    },

    #[cfg(feature = "python")]
    #[error(
        "python plugin '{name}': module '{module}' is missing class '{class}'\n\
         hint: your plugin must define `class {class}` with __init__(self, settings: dict)\n\
         python error: {detail}"
    )]
    PythonMissingClass {
        name: String,
        module: String,
        class: String,
        detail: String,
    },

    #[cfg(feature = "python")]
    #[error(
        "python plugin '{name}': error calling '{method}' on '{class}'\n\
         hint: check the method signature and return type. \
         Expected: {expected_signature}\n\
         python error: {detail}"
    )]
    PythonMethodCallFailed {
        name: String,
        class: String,
        method: String,
        expected_signature: String,
        detail: String,
    },

    #[cfg(feature = "python")]
    #[error(
        "python plugin '{name}': failed to convert return value from '{method}'\n\
         hint: '{method}' must return {expected_type}\n\
         python error: {detail}"
    )]
    PythonReturnTypeError {
        name: String,
        method: String,
        expected_type: String,
        detail: String,
    },

    #[cfg(feature = "python")]
    #[error(
        "python plugin '{name}': anomaly dict at index {index} is missing required key '{key}'\n\
         hint: each dict in the list returned by detect() must have 'pattern' (str) and 'count' (int)"
    )]
    PythonAnomalyMissingKey {
        name: String,
        index: usize,
        key: String,
    },

    #[cfg(feature = "python")]
    #[error(
        "python plugin '{name}': failed to parse remediator response as JSON: {source}\n\
         hint: propose() must return a JSON string (str), got something else"
    )]
    PythonJsonParseError {
        name: String,
        source: serde_json::Error,
    },

    #[cfg(feature = "python")]
    #[error(
        "python plugin '{name}': failed to access Python sys.path: {detail}\n\
         hint: this is usually a PyO3 initialization issue"
    )]
    PythonSysPathError { name: String, detail: String },

    #[cfg(not(feature = "python"))]
    #[error(
        "plugin '{name}': Python plugin support is not enabled\n\
         hint: rebuild logmedic with `cargo build --features python`"
    )]
    PythonNotEnabled { name: String },

    // ── Config / general errors ─────────────────────────────────────
    #[error(
        "remediator '{name}': missing required setting 'path' in [remediators.settings]\n\
         hint: add `path = \"plugins/my_remediator/my_remediator.py\"` to the config"
    )]
    MissingPluginPath { name: String },

    #[error("plugin '{name}': failed to serialize settings to JSON: {source}")]
    SettingsSerializationFailed {
        name: String,
        source: serde_json::Error,
    },

    #[cfg_attr(not(feature = "python"), allow(dead_code))]
    #[error("plugin '{name}': background task panicked or was cancelled: {source}")]
    TaskJoinError {
        name: String,
        source: tokio::task::JoinError,
    },
}
