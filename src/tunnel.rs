//! Python bindings of the Restate Cloud tunnel engine and keys.

use std::time::Duration;

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use restate_sdk_shared_core::cloud::{
    ConfigSource, ConnectOptions, IngressKey, ReconnectPolicy, Servers, Tuning,
    TunnelEngineOptions, INGRESS_KEY_ENV, INGRESS_KEY_URL,
};
use restate_sdk_shared_core::tunnel::{self, TunnelEngine};

use crate::asgi::AsgiService;

create_exception!(
    _internal,
    ConnectError,
    PyException,
    "Raised when the deployment cannot connect to Restate Cloud: a configuration problem, \
     rejected credentials, or an unrecoverable connection failure."
);

fn connect_error(error: impl std::fmt::Display) -> PyErr {
    ConnectError::new_err(error.to_string())
}

/// Everything the engine needs, resolved from code and the environment.
#[pyclass(name = "TunnelEngineOptions", frozen)]
pub struct PyTunnelEngineOptions {
    inner: TunnelEngineOptions,
}

#[pymethods]
impl PyTunnelEngineOptions {
    /// Resolve the options. Every argument is optional and falls back to the environment
    /// (`RESTATE_CONNECT_NAME`, `RESTATE_CONNECT_KEY`, or the operator's `RESTATE_INPROC_*`).
    #[staticmethod]
    #[pyo3(signature = (name=None, key=None, servers=None, servers_srv=None, worker_id=None, tuning=None))]
    fn resolve(
        name: Option<String>,
        key: Option<String>,
        servers: Option<Vec<String>>,
        servers_srv: Option<String>,
        worker_id: Option<String>,
        tuning: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        if servers.is_some() && servers_srv.is_some() {
            return Err(PyValueError::new_err(
                "specify at most one of servers and servers_srv",
            ));
        }
        let servers = match (servers, servers_srv) {
            (Some(servers), _) => Some(Servers::Explicit(servers)),
            (None, Some(srv)) => Some(Servers::Srv(srv)),
            (None, None) => None,
        };
        let tuning = match tuning {
            Some(tuning) => parse_tuning(tuning)?,
            None => Tuning::default(),
        };
        let inner = TunnelEngineOptions::resolve(ConnectOptions {
            name,
            key,
            token: None,
            servers,
            worker_id,
            tuning,
        })
        .map_err(connect_error)?;
        Ok(Self { inner })
    }

    #[getter]
    fn name(&self) -> &str {
        &self.inner.name
    }

    #[getter]
    fn environment_id(&self) -> &str {
        &self.inner.environment_id
    }

    #[getter]
    fn region(&self) -> &str {
        &self.inner.region
    }

    /// Where the coordinates came from: "code", "connect_key" or "operator".
    #[getter]
    fn source(&self) -> &'static str {
        match self.inner.source {
            ConfigSource::Code => "code",
            ConfigSource::ConnectKey => "connect_key",
            ConfigSource::Operator => "operator",
        }
    }

    /// `connect://<name>`: what to register with Restate.
    #[getter]
    fn registration_identifier(&self) -> String {
        self.inner.registration_identifier()
    }

    /// The Cloud UI link that opens the register dialog pre-filled.
    #[getter]
    fn registration_deep_link(&self) -> String {
        self.inner.registration_deep_link()
    }

    fn __repr__(&self) -> String {
        format!("{:?}", self.inner)
    }
}

fn parse_tuning(tuning: &Bound<'_, PyDict>) -> PyResult<Tuning> {
    let mut result = Tuning::default();
    let mut reconnect = ReconnectPolicy::default();
    for (key, value) in tuning.iter() {
        let key: String = key.extract()?;
        match key.as_str() {
            "connect_timeout" => result.connect_timeout = seconds(&value)?,
            "handshake_timeout" => result.handshake_timeout = seconds(&value)?,
            "ping_interval" => result.ping_interval = seconds(&value)?,
            "ping_timeout" => result.ping_timeout = seconds(&value)?,
            "server_drain_grace" => result.server_drain_grace = seconds(&value)?,
            "max_concurrent_streams" => result.max_concurrent_streams = value.extract()?,
            "reconnect_initial_interval" => reconnect.initial_interval = seconds(&value)?,
            "reconnect_max_interval" => reconnect.max_interval = seconds(&value)?,
            "reconnect_factor" => reconnect.factor = value.extract()?,
            "reconnect_max_attempts" => reconnect.max_attempts = value.extract()?,
            "reconnect_max_duration" => {
                reconnect.max_duration = if value.is_none() {
                    None
                } else {
                    Some(seconds(&value)?)
                }
            }
            other => {
                return Err(PyValueError::new_err(format!(
                    "unknown tuning option {other:?}"
                )))
            }
        }
    }
    result.reconnect = reconnect;
    Ok(result)
}

fn seconds(value: &Bound<'_, PyAny>) -> PyResult<Duration> {
    let seconds: f64 = value.extract()?;
    if !seconds.is_finite() || seconds < 0.0 {
        return Err(PyValueError::new_err(format!(
            "durations are non-negative seconds, got {seconds}"
        )));
    }
    Ok(Duration::from_secs_f64(seconds))
}

/// Metadata learned from a successful Cloud handshake.
#[pyclass(name = "TunnelInfo", frozen, get_all)]
#[derive(Clone)]
pub struct PyTunnelInfo {
    name: String,
    proxy_url: String,
    tunnel_url: String,
    deployment_url: String,
}

impl From<tunnel::TunnelInfo> for PyTunnelInfo {
    fn from(info: tunnel::TunnelInfo) -> Self {
        Self {
            name: info.name,
            proxy_url: info.proxy_url,
            tunnel_url: info.tunnel_url,
            deployment_url: info.deployment_url,
        }
    }
}

#[pymethods]
impl PyTunnelInfo {
    fn __repr__(&self) -> String {
        format!(
            "TunnelInfo(name={:?}, proxy_url={:?}, tunnel_url={:?}, deployment_url={:?})",
            self.name, self.proxy_url, self.tunnel_url, self.deployment_url
        )
    }
}

/// A running tunnel engine serving an ASGI application. Must be created inside a
/// running asyncio event loop: the application is called on that loop.
///
/// Dropping the last reference cuts every connection at once.
#[pyclass(name = "TunnelEngine", frozen)]
pub struct PyTunnelEngine {
    engine: TunnelEngine,
}

#[pymethods]
impl PyTunnelEngine {
    #[new]
    fn new(py: Python<'_>, app: Py<PyAny>, options: &PyTunnelEngineOptions) -> PyResult<Self> {
        let locals = pyo3_async_runtimes::tokio::get_current_locals(py).map_err(|_| {
            PyRuntimeError::new_err(
                "TunnelEngine must be created inside a running asyncio event loop",
            )
        })?;
        let service = AsgiService::new(app, locals);
        let runtime = pyo3_async_runtimes::tokio::get_runtime();
        let engine = {
            let _guard = runtime.enter();
            TunnelEngine::start(service, options.inner.clone())
        }
        .map_err(connect_error)?;
        Ok(Self { engine })
    }

    /// Await the first successful handshake; returns the `TunnelInfo`.
    /// Raises `ConnectError` if the engine stops first.
    fn ready<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let engine = self.engine.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            engine
                .ready()
                .await
                .map(PyTunnelInfo::from)
                .map_err(connect_error)
        })
    }

    /// Await the engine's end: returns after a shutdown, raises `ConnectError` after a
    /// fatal error.
    fn terminated<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let engine = self.engine.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            engine.terminated().await.map_err(connect_error)
        })
    }

    /// Stop serving: refuse new invocations, let the in-flight ones finish, and cut
    /// whatever is still running after `grace_seconds`. Zero cuts everything at once.
    /// Teardown begins before the returned awaitable is awaited.
    fn shutdown<'py>(&self, py: Python<'py>, grace_seconds: f64) -> PyResult<Bound<'py, PyAny>> {
        if !grace_seconds.is_finite() || grace_seconds < 0.0 {
            return Err(PyValueError::new_err(
                "grace_seconds must be a non-negative number of seconds",
            ));
        }
        let shutdown = self.engine.shutdown(Duration::from_secs_f64(grace_seconds));
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            shutdown.await.map_err(connect_error)
        })
    }
}

/// A parsed `rstcloudv1_…` ingress key.
#[pyclass(name = "IngressKey", frozen, get_all)]
pub struct PyIngressKey {
    environment_id: String,
    api_key: String,
    region: String,
}

#[pymethods]
impl PyIngressKey {
    /// Parse an ingress key. Raises `ConnectError` with the text to show the user.
    #[staticmethod]
    #[pyo3(signature = (value, what="the ingress key"))]
    fn parse(value: &str, what: &str) -> PyResult<Self> {
        IngressKey::parse(value)
            .map(Self::from)
            .map_err(|error| ConnectError::new_err(key_error(what, &error)))
    }

    /// Read and parse `RESTATE_INGRESS_KEY`; `None` when it is unset or empty.
    #[staticmethod]
    fn from_env() -> PyResult<Option<Self>> {
        IngressKey::from_env()
            .map(|key| key.map(Self::from))
            .map_err(|error| ConnectError::new_err(key_error(INGRESS_KEY_ENV, &error)))
    }

    /// `https://<id>.env.<region>:8080`
    fn ingress_url(&self) -> String {
        self.key().ingress_url()
    }

    /// The `Authorization` header value.
    fn bearer(&self) -> String {
        self.key().bearer()
    }

    fn __repr__(&self) -> String {
        format!(
            "IngressKey(environment_id={:?}, region={:?}, api_key=[REDACTED])",
            self.environment_id, self.region
        )
    }
}

impl PyIngressKey {
    fn key(&self) -> IngressKey {
        IngressKey {
            environment_id: self.environment_id.clone(),
            api_key: self.api_key.clone(),
            region: self.region.clone(),
        }
    }
}

impl From<IngressKey> for PyIngressKey {
    fn from(key: IngressKey) -> Self {
        Self {
            environment_id: key.environment_id,
            api_key: key.api_key,
            region: key.region,
        }
    }
}

/// The Cloud UI page where ingress keys are created.
pub const INGRESS_KEY_HELP_URL: &str = INGRESS_KEY_URL;

fn key_error(what: &str, error: &restate_sdk_shared_core::cloud::KeyError) -> String {
    format!("{what} {error} — copy it again from {INGRESS_KEY_URL}")
}
