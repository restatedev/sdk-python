//! For tests only (feature `fake_runtime`): shared-core's fake Restate runtime for the explicit
//! recovery protocol, `explicit_recovery::fake_runtime`, as the class `_FakeRuntime`.
//!
//! One invocation, over as many attempts as it takes: bytes in (`receive`, what the SDK wrote),
//! bytes out (`take_output`, what the SDK reads). It applies what it receives, answers, decides
//! between restart and suspend after a shutdown, and checks everything the SDK sends against the
//! protocol: a violation raises `FakeRuntimeViolation` and is kept in `violations()`.
use crate::explicit_recovery::value_to_py;
use crate::PyFailure;
use bytes::Bytes;
use pyo3::create_exception;
use pyo3::exceptions::{PyAssertionError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use restate_sdk_shared_core::explicit_recovery::fake_runtime::{
    self, request_headers, AttemptOutcome, CalleeBehavior, FakeRuntime, FakeRuntimeError,
    FakeRuntimeOptions, InvocationSpec, InvocationStatus, Preload, ShutdownReason,
};
use restate_sdk_shared_core::Value;

create_exception!(
    restate_sdk_python_core,
    FakeRuntimeViolation,
    PyAssertionError,
    "The SDK violated the explicit recovery protocol, as the fake runtime found: the attempt failed."
);

fn fake_error(error: FakeRuntimeError) -> PyErr {
    match error {
        FakeRuntimeError::Violation(what) => FakeRuntimeViolation::new_err(what),
        FakeRuntimeError::Misuse(what) => {
            PyValueError::new_err(format!("fake runtime misuse: {what}"))
        }
    }
}

/// A preload policy from Python: `"all"`, `"lazy"`, or a list of the keys to preload.
fn preload_of(policy: &Bound<'_, PyAny>) -> PyResult<Preload> {
    if let Ok(keys) = policy.extract::<Vec<String>>() {
        if !policy.is_instance_of::<pyo3::types::PyString>() {
            return Ok(Preload::Eager(keys));
        }
    }
    match policy.extract::<String>()?.as_str() {
        "all" => Ok(Preload::All),
        "lazy" => Ok(Preload::Lazy),
        other => Err(PyValueError::new_err(format!(
            "a preload policy is \"all\", \"lazy\" or a list of keys, not {other:?}"
        ))),
    }
}

/// A value from Python: `None` (void), `bytes` (a success) or a `PyFailure`.
fn value_of(value: Option<&Bound<'_, PyAny>>) -> PyResult<Value> {
    let Some(value) = value.filter(|value| !value.is_none()) else {
        return Ok(Value::Void);
    };
    if let Ok(bytes) = value.cast::<PyBytes>() {
        return Ok(Value::Success(Bytes::copy_from_slice(bytes.as_bytes())));
    }
    let failure: PyFailure = value.extract()?;
    Ok(Value::Failure(failure.into()))
}

#[pyclass(name = "_FakeRuntime")]
pub(crate) struct PyFakeRuntime(FakeRuntime);

#[pymethods]
impl PyFakeRuntime {
    /// `object_preload`, `invocation_preload`: `"all"`, `"lazy"`, or the keys to preload.
    #[new]
    #[pyo3(signature = (
        *,
        invocation_id = None,
        service = "FakeObject".to_owned(),
        handler = "handle".to_owned(),
        key = Some("key".to_owned()),
        shared = false,
        input = None,
        headers = vec![],
        idempotency_key = None,
        scope = None,
        limit_key = None,
        object_preload = None,
        invocation_preload = None,
        first_next_result_id = 1,
        max_consecutive_failures = None,
        max_idle_restarts = 3,
        features = vec![],
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        invocation_id: Option<String>,
        service: String,
        handler: String,
        key: Option<String>,
        shared: bool,
        input: Option<Vec<u8>>,
        headers: Vec<(String, String)>,
        idempotency_key: Option<String>,
        scope: Option<String>,
        limit_key: Option<String>,
        object_preload: Option<&Bound<'_, PyAny>>,
        invocation_preload: Option<&Bound<'_, PyAny>>,
        first_next_result_id: u32,
        max_consecutive_failures: Option<u32>,
        max_idle_restarts: u32,
        features: Vec<u32>,
    ) -> PyResult<Self> {
        if first_next_result_id == 0 {
            return Err(PyValueError::new_err("first_next_result_id is at least 1"));
        }
        let spec = InvocationSpec {
            invocation_id: invocation_id.unwrap_or_else(|| fake_runtime::invocation_id(0)),
            service,
            handler,
            key,
            shared,
            input: Bytes::from(input.unwrap_or_default()),
            headers,
            idempotency_key,
            scope,
            limit_key,
            object_preload: object_preload
                .map(preload_of)
                .transpose()?
                .unwrap_or_default(),
            invocation_preload: invocation_preload
                .map(preload_of)
                .transpose()?
                .unwrap_or_default(),
        };
        let options = FakeRuntimeOptions {
            first_next_result_id,
            max_consecutive_failures,
            max_idle_restarts,
            features,
            ..Default::default()
        };
        Ok(Self(FakeRuntime::with_options(spec, options)))
    }

    /// An invocation id in Restate's format, the `n`-th the fake mints: 0 is the invocation's own
    /// by default, its calls' are 1, 2, … A test takes one above those for an invocation the fake
    /// does not run (a signal's target, say).
    #[staticmethod]
    fn invocation_id(n: u64) -> String {
        fake_runtime::invocation_id(n)
    }

    /// The headers of the request that opens an attempt.
    #[staticmethod]
    fn request_headers() -> Vec<(String, String)> {
        request_headers()
    }

    fn content_type(&self) -> &'static str {
        self.0.content_type()
    }

    // --- Setting the scene

    fn set_object_state(&mut self, key: String, value: Vec<u8>) {
        self.0.object_state_mut().insert(key, Bytes::from(value));
    }

    fn set_invocation_state(&mut self, key: String, value: Vec<u8>) {
        self.0
            .invocation_state_mut()
            .insert(key, Bytes::from(value));
    }

    /// What a call to `service`/`handler` does: `"pending"` (until `complete_call`),
    /// `"not_found"`, or `"returns"` with `value` (`None` for void, `bytes`, or a `PyFailure`).
    #[pyo3(signature = (service, handler, behavior, value=None))]
    fn script_callee(
        &mut self,
        service: String,
        handler: String,
        behavior: &str,
        value: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let behavior = match behavior {
            "pending" => CalleeBehavior::Pending,
            "not_found" => CalleeBehavior::NotFound,
            "returns" => CalleeBehavior::Returns(value_of(value)?),
            other => {
                return Err(PyValueError::new_err(format!(
                "a callee's behavior is \"pending\", \"returns\" or \"not_found\", not {other:?}"
            )))
            }
        };
        self.0.script_callee(service, handler, behavior);
        Ok(())
    }

    // --- Observing

    /// `"ready"`, `"running"`, `"suspended"`, `"paused"`, or `("completed", value)`.
    fn status(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(match self.0.status() {
            InvocationStatus::Ready => "ready".into_pyobject(py)?.into_any().unbind(),
            InvocationStatus::Running => "running".into_pyobject(py)?.into_any().unbind(),
            InvocationStatus::Suspended => "suspended".into_pyobject(py)?.into_any().unbind(),
            InvocationStatus::Paused => "paused".into_pyobject(py)?.into_any().unbind(),
            InvocationStatus::Completed(value) => ("completed", value_to_py(py, value.clone())?)
                .into_pyobject(py)?
                .into_any()
                .unbind(),
        })
    }

    fn object_state(&self) -> Vec<(String, Vec<u8>)> {
        (self.0.object_state().iter())
            .map(|(k, v)| (k.clone(), v.to_vec()))
            .collect()
    }

    fn invocation_state(&self) -> Vec<(String, Vec<u8>)> {
        (self.0.invocation_state().iter())
            .map(|(k, v)| (k.clone(), v.to_vec()))
            .collect()
    }

    /// The outcome of each attempt that ended: `("completed",)`, `("failed_invocation", code)`,
    /// `("paused", code)`, `("failed", reason)`, `("restarted", rule)`, `("lost",)`,
    /// `("suspended",)`.
    fn outcomes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let outcomes = PyList::empty(py);
        for outcome in self.0.outcomes() {
            let item = match outcome {
                AttemptOutcome::Completed => ("completed",).into_pyobject(py)?.into_any(),
                AttemptOutcome::FailedInvocation { code } => {
                    ("failed_invocation", *code).into_pyobject(py)?.into_any()
                }
                AttemptOutcome::Paused { code } => ("paused", *code).into_pyobject(py)?.into_any(),
                AttemptOutcome::Failed { reason } => {
                    ("failed", reason.clone()).into_pyobject(py)?.into_any()
                }
                AttemptOutcome::Restarted { rule } => {
                    ("restarted", *rule).into_pyobject(py)?.into_any()
                }
                AttemptOutcome::Lost => ("lost",).into_pyobject(py)?.into_any(),
                AttemptOutcome::Suspended => ("suspended",).into_pyobject(py)?.into_any(),
            };
            outcomes.append(item)?;
        }
        Ok(outcomes)
    }

    fn attempt_number(&self) -> u32 {
        self.0.attempt_number()
    }

    fn next_result_id(&self) -> u32 {
        self.0.next_result_id()
    }

    /// Every invocation a `Call` made, sends included, in order: dicts with `invocation_id`,
    /// `service`, `handler`, `key`, `idempotency_key`, `parameter`, `invoke_time`, `attempt` and
    /// `output` (`None` until it returned).
    fn calls<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let calls = PyList::empty(py);
        for call in self.0.calls() {
            let record = PyDict::new(py);
            record.set_item("invocation_id", &call.invocation_id)?;
            record.set_item("service", &call.service)?;
            record.set_item("handler", &call.handler)?;
            record.set_item("key", &call.key)?;
            record.set_item("idempotency_key", &call.idempotency_key)?;
            record.set_item("parameter", PyBytes::new(py, &call.parameter))?;
            record.set_item("invoke_time", call.invoke_time)?;
            record.set_item("attempt", call.attempt)?;
            let output = match &call.output {
                Some(output) => Some(value_to_py(py, output.clone())?),
                None => None,
            };
            record.set_item("output", output)?;
            calls.append(record)?;
        }
        Ok(calls)
    }

    /// `(target_invocation_id, name, value)` of each signal an action sent.
    fn signals_sent(&self, py: Python<'_>) -> PyResult<Vec<(String, String, Py<PyAny>)>> {
        (self.0.signals_sent().iter())
            .map(|signal| {
                Ok((
                    signal.target_invocation_id.clone(),
                    signal.name.clone(),
                    value_to_py(py, signal.value.clone())?,
                ))
            })
            .collect()
    }

    fn cancels_sent(&self) -> Vec<String> {
        self.0.cancels_sent().to_vec()
    }

    fn violations(&self) -> Vec<String> {
        self.0.violations().to_vec()
    }

    fn last_retry_delay(&self) -> Option<u64> {
        self.0.last_retry_delay()
    }

    fn is_closed(&self) -> bool {
        self.0.is_closed()
    }

    fn is_attempt_running(&self) -> bool {
        self.0.is_attempt_running()
    }

    // --- The attempt

    fn start_attempt<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let start = self.0.start_attempt().map_err(fake_error)?;
        Ok(PyBytes::new(py, &start))
    }

    fn receive(&mut self, bytes: &Bound<'_, PyBytes>) -> PyResult<()> {
        self.0
            .receive(Bytes::copy_from_slice(bytes.as_bytes()))
            .map_err(fake_error)
    }

    fn take_output<'py>(&mut self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.0.take_output())
    }

    fn set_auto_apply(&mut self, auto_apply: bool) {
        self.0.set_auto_apply(auto_apply);
    }

    fn apply_pending(&mut self, max: usize) -> PyResult<usize> {
        self.0.apply_pending(max).map_err(fake_error)
    }

    /// `reason`: `"inactivity"` (the attempt must be silent) or `"own"`.
    fn shut_down(&mut self, reason: &str) -> PyResult<()> {
        let reason = match reason {
            "inactivity" => ShutdownReason::Inactivity,
            "own" => ShutdownReason::Own,
            other => {
                return Err(PyValueError::new_err(format!(
                    "a shutdown's reason is \"inactivity\" or \"own\", not {other:?}"
                )))
            }
        };
        self.0.shut_down(reason).map_err(fake_error)
    }

    fn shutdown_answer_timed_out(&mut self) -> PyResult<()> {
        self.0.shutdown_answer_timed_out().map_err(fake_error)
    }

    fn sdk_closed(&mut self) -> PyResult<()> {
        self.0.sdk_closed().map_err(fake_error)
    }

    fn break_stream(&mut self) -> PyResult<()> {
        self.0.break_stream().map_err(fake_error)
    }

    fn lose_attempt(&mut self) -> PyResult<()> {
        self.0.lose_attempt().map_err(fake_error)
    }

    // --- The world

    /// `output`: `None` (void), `bytes`, or a `PyFailure`.
    #[pyo3(signature = (invocation_id, output=None))]
    fn complete_call(
        &mut self,
        invocation_id: &str,
        output: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        self.0
            .complete_call(invocation_id, value_of(output)?)
            .map_err(fake_error)
    }

    /// `value`: `None` (void), `bytes`, or a `PyFailure`.
    #[pyo3(signature = (name, value=None))]
    fn send_signal(&mut self, name: &str, value: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        self.0.send_signal(name, value_of(value)?);
        Ok(())
    }

    fn cancel(&mut self) {
        self.0.cancel();
    }

    fn resume(&mut self) -> PyResult<()> {
        self.0.resume().map_err(fake_error)
    }
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyFakeRuntime>()?;
    m.add(
        "FakeRuntimeViolation",
        m.py().get_type::<FakeRuntimeViolation>(),
    )?;
    Ok(())
}
