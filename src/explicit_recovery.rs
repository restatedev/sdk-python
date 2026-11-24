//! The VM of a session object's attempt, on the explicit recovery protocol (version 1):
//! shared-core's [`CoreExplicitRecoveryVM`], which has no journal.
//!
//! An action returns a handle, and its result comes back as a notification of that handle
//! (`take_notification`): `restate/sessions/_driver.py` settles its futures with them, in its
//! tick, which is the only place notifications are taken. Taking one is processing it: it counts
//! in the `ShutdownMessage` the VM writes when the runtime shuts the attempt down. The database's
//! store ([`crate::sessions`]) awaits some handles from Rust: it registers a waiter
//! ([`PyExplicitRecoveryVM::wait_in_rust`]), and the tick hands it the notification as it takes it.
//!
//! The cancel signal comes on the reserved handle 1 (`CANCEL_NOTIFICATION_HANDLE`), which no
//! action has: after it, `is_cancelled()` is true.
use crate::{
    PyCallHandle, PyFailure, PyHeader, PyInput, PyResponseHead, PyStateKeys, PyVoid, VMException,
};
use futures::channel::oneshot;
use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};
use restate_sdk_shared_core::explicit_recovery::{
    ActionOptions, AttemptState, CoreExplicitRecoveryVM, ExplicitRecoveryVM, Notification, Options,
    ProtocolVersion, RecordLevel, SpanId, StateScope,
};
use restate_sdk_shared_core::{
    AttachInvocationTarget, Error, NonEmptyValue, NotificationHandle, Target, Value,
};
use std::collections::HashMap;
use std::time::{Duration, SystemTime};

create_exception!(
    restate_sdk_python_core,
    AttemptShutDownException,
    pyo3::exceptions::PyException,
    "Restate shut the attempt down: it is over, and a new attempt will run the handler from the start."
);

/// A VM error as Python gets it: the attempt's shutdown is an `AttemptShutDownException`; any
/// other a `VMException` whose args are `(message, code)`: a refused `new` answers the request
/// with that code as its HTTP status (415), not with a 500.
pub(crate) fn vm_error(error: Error) -> PyErr {
    if error.is_suspended_error() {
        AttemptShutDownException::new_err(error.to_string())
    } else {
        VMException::new_err((error.to_string(), error.code()))
    }
}

/// Where Rust waits for the notification of a handle.
type RustWaiter = oneshot::Sender<Result<Value, Error>>;

/// The scope of a state action, from Python: `"object"` or `"invocation"`.
fn scope_of(scope: &str) -> PyResult<StateScope> {
    match scope {
        "object" => Ok(StateScope::Object),
        "invocation" => Ok(StateScope::Invocation),
        other => Err(PyValueError::new_err(format!(
            "a state scope is \"object\" or \"invocation\", not {other:?}"
        ))),
    }
}

/// The record level of an action, from Python: `None` (the runtime's default), `"none"`,
/// `"summary"` or `"full"`.
fn record_of(record: Option<&str>) -> PyResult<RecordLevel> {
    match record {
        None | Some("default") => Ok(RecordLevel::Default),
        Some("none") => Ok(RecordLevel::None),
        Some("summary") => Ok(RecordLevel::Summary),
        Some("full") => Ok(RecordLevel::Full),
        Some(other) => Err(PyValueError::new_err(format!(
            "a record level is \"default\", \"none\", \"summary\" or \"full\", not {other:?}"
        ))),
    }
}

fn span_of(span: u32) -> PyResult<SpanId> {
    SpanId::try_from(span).map_err(|_| PyValueError::new_err("a span id is never 0"))
}

/// The options of an action from its keywords: `name`, `parent_span`, `record`.
pub(crate) fn options_of(
    name: Option<String>,
    parent_span: Option<u32>,
    record: Option<&str>,
) -> PyResult<ActionOptions> {
    let mut options = ActionOptions::default().recorded(record_of(record)?);
    options.name = name;
    if let Some(parent) = parent_span {
        options = options.inside(span_of(parent)?);
    }
    Ok(options)
}

/// A value Python passes for a span's end or a signal: `None` (nothing), `bytes`, or `PyFailure`.
fn non_empty_value_of(value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<NonEmptyValue>> {
    let Some(value) = value.filter(|value| !value.is_none()) else {
        return Ok(None);
    };
    if let Ok(bytes) = value.cast::<PyBytes>() {
        return Ok(Some(NonEmptyValue::Success(
            bytes.as_bytes().to_vec().into(),
        )));
    }
    let failure: PyFailure = value.extract()?;
    Ok(Some(NonEmptyValue::Failure(failure.into())))
}

/// `invoke_at` from a delay in milliseconds: the time since the UNIX epoch at which to invoke.
fn invoke_at(delay: Option<u64>) -> Option<Duration> {
    delay.map(|millis| {
        SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .expect("Duration since unix epoch cannot fail")
            + Duration::from_millis(millis)
    })
}

#[pyclass]
pub(crate) struct PyExplicitRecoveryVM {
    pub(crate) vm: CoreExplicitRecoveryVM,
    /// The handles Rust waits for (the database's reads and commits), with their waiter.
    rust_waiters: HashMap<NotificationHandle, RustWaiter>,
    /// Why the attempt ended, once `take_next_notification` said so: every later waiter gets it.
    ended: Option<Error>,
    /// The handles of aborted atomic groups' actions, which will never be notified: Python
    /// fails their futures ([`PyExplicitRecoveryVM::take_aborted`]).
    aborted: Vec<NotificationHandle>,
    /// What Rust calls after its own calls to the VM: it schedules Python's tick, which writes
    /// the output and settles Python's futures.
    scheduler: Option<Py<PyAny>>,
}

impl PyExplicitRecoveryVM {
    /// The notification of `handle`, for Rust: registered now, handed over by the tick that takes
    /// it. Once the attempt is over, at once with why.
    pub(crate) fn wait_in_rust(
        &mut self,
        handle: NotificationHandle,
    ) -> oneshot::Receiver<Result<Value, Error>> {
        let (sender, receiver) = oneshot::channel();
        if let Some(error) = &self.ended {
            let _ = sender.send(Err(error.clone()));
        } else {
            self.rust_waiters.insert(handle, sender);
        }
        receiver
    }

    /// Record handles that will never be notified (an aborted group's).
    pub(crate) fn forget(&mut self, handles: Vec<NotificationHandle>) {
        for handle in &handles {
            if let Some(waiter) = self.rust_waiters.remove(handle) {
                let _ = waiter.send(Err(Error::new(
                    restate_sdk_shared_core::error::codes::INTERNAL,
                    "the atomic group of this action was aborted",
                )));
            }
        }
        self.aborted.extend(handles);
    }

    pub(crate) fn scheduler(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.scheduler
            .as_ref()
            .map(|scheduler| scheduler.clone_ref(py))
    }
}

fn target(
    service: String,
    handler: String,
    key: Option<String>,
    idempotency_key: Option<String>,
    headers: Option<Vec<PyHeader>>,
) -> Target {
    Target {
        service,
        handler,
        key,
        idempotency_key,
        scope: None,
        limit_key: None,
        headers: headers
            .unwrap_or_default()
            .into_iter()
            .map(Into::into)
            .collect(),
    }
}

/// A notification's value as Python gets it: `PyVoid`, `bytes`, `PyFailure`, `PyStateKeys`, or the
/// invocation id as `str`.
pub(crate) fn value_to_py(py: Python<'_>, value: Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::Void => Py::new(py, PyVoid)?.into_any(),
        Value::Success(b) => PyBytes::new(py, &b).into_any().unbind(),
        Value::Failure(f) => Py::new(py, PyFailure::from(f))?.into_any(),
        Value::StateKeys(keys) => Py::new(py, PyStateKeys { keys })?.into_any(),
        Value::InvocationId(id) => PyString::new(py, &id).into_any().unbind(),
    })
}

fn handle_of(handle: Option<NotificationHandle>) -> Option<u32> {
    handle.map(u32::from)
}

/// The supported range of the explicit recovery protocol: `(min, max)`, for discovery.
#[pyfunction]
pub(crate) fn explicit_recovery_protocol_range() -> (u32, u32) {
    (
        ProtocolVersion::minimum_supported_version() as u32,
        ProtocolVersion::maximum_supported_version() as u32,
    )
}

#[pymethods]
impl PyExplicitRecoveryVM {
    /// Raises `VMException(message, code)` with the code to answer the request with (415 for a
    /// content type of another family or version), and nothing written.
    #[new]
    fn new(headers: Vec<(String, String)>) -> PyResult<Self> {
        let vm = CoreExplicitRecoveryVM::new(headers, Options::default()).map_err(vm_error)?;
        Ok(Self {
            vm,
            rust_waiters: Default::default(),
            ended: None,
            aborted: vec![],
            scheduler: None,
        })
    }

    fn set_scheduler(&mut self, scheduler: Py<PyAny>) {
        self.scheduler = Some(scheduler);
    }

    fn get_response_head(&self) -> PyResponseHead {
        self.vm.get_response_head().into()
    }

    fn notify_input(&mut self, buffer: &Bound<'_, PyBytes>) {
        self.vm.notify_input(buffer.as_bytes().to_vec().into());
    }

    fn notify_input_closed(&mut self) {
        self.vm.notify_input_closed();
    }

    fn take_output<'py>(&mut self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.vm.take_output())
    }

    /// Whether to close the response now: the VM wrote its last message, and the runtime closed
    /// its side. Write `take_output()` first.
    fn should_close_output(&self) -> bool {
        self.vm.should_close_output()
    }

    fn is_ready_to_execute(&self) -> PyResult<bool> {
        self.vm.is_ready_to_execute().map_err(vm_error)
    }

    fn input(&mut self) -> PyResult<PyInput> {
        self.vm.get_input().map(Into::into).map_err(vm_error)
    }

    fn is_feature_supported(&self, feature: u32) -> bool {
        self.vm.is_feature_supported(feature)
    }

    /// The next notification, `(handle, value)`, or `None`: only the driver's tick calls it, and
    /// settles the future of each before it yields. A handle Rust waits for goes to its waiter,
    /// and the next one is taken. Raises once the attempt is over: `AttemptShutDownException` for
    /// a shutdown, `VMException` for a failure (the VM wrote `ErrorMessage`).
    fn take_notification(&mut self, py: Python<'_>) -> PyResult<Option<(u32, Py<PyAny>)>> {
        loop {
            match self.vm.take_next_notification() {
                Ok(Some(Notification { handle, value })) => {
                    match self.rust_waiters.remove(&handle) {
                        Some(waiter) => {
                            let _ = waiter.send(Ok(value));
                        }
                        None => return Ok(Some((u32::from(handle), value_to_py(py, value)?))),
                    }
                }
                Ok(None) => return Ok(None),
                Err(error) => {
                    for (_, waiter) in self.rust_waiters.drain() {
                        let _ = waiter.send(Err(error.clone()));
                    }
                    self.ended = Some(error.clone());
                    return Err(vm_error(error));
                }
            }
        }
    }

    /// The handles of aborted atomic groups' actions since the last call: never notified.
    fn take_aborted(&mut self) -> Vec<u32> {
        self.aborted.drain(..).map(u32::from).collect()
    }

    // --- State

    #[pyo3(signature = (scope, key, *, name=None, parent_span=None, record=None))]
    fn get_state(
        &mut self,
        scope: &str,
        key: String,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<u32> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .cmd_state_get(scope_of(scope)?, key, options)
            .map(u32::from)
            .map_err(vm_error)
    }

    #[pyo3(signature = (scope, *, name=None, parent_span=None, record=None))]
    fn get_state_keys(
        &mut self,
        scope: &str,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<u32> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .cmd_state_get_keys(scope_of(scope)?, options)
            .map(u32::from)
            .map_err(vm_error)
    }

    /// Returns the handle of the result, `None` without `want_result`.
    #[pyo3(signature = (scope, key, buffer, *, want_result=false, name=None, parent_span=None, record=None))]
    #[allow(clippy::too_many_arguments)]
    fn set_state(
        &mut self,
        scope: &str,
        key: String,
        buffer: &Bound<'_, PyBytes>,
        want_result: bool,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<Option<u32>> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .cmd_state_set(
                scope_of(scope)?,
                key,
                buffer.as_bytes().to_vec().into(),
                options,
                want_result,
            )
            .map(handle_of)
            .map_err(vm_error)
    }

    #[pyo3(signature = (scope, key, *, want_result=false, name=None, parent_span=None, record=None))]
    fn clear_state(
        &mut self,
        scope: &str,
        key: String,
        want_result: bool,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<Option<u32>> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .cmd_state_clear(scope_of(scope)?, key, options, want_result)
            .map(handle_of)
            .map_err(vm_error)
    }

    #[pyo3(signature = (scope, *, want_result=false, name=None, parent_span=None, record=None))]
    fn clear_all_state(
        &mut self,
        scope: &str,
        want_result: bool,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<Option<u32>> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .cmd_state_clear_all(scope_of(scope)?, options, want_result)
            .map(handle_of)
            .map_err(vm_error)
    }

    // --- Invocations

    /// `delay` is in milliseconds.
    #[pyo3(signature = (service, handler, buffer, key=None, idempotency_key=None, headers=None, delay=None, *, name=None, parent_span=None, record=None))]
    #[allow(clippy::too_many_arguments)]
    fn call(
        &mut self,
        service: String,
        handler: String,
        buffer: &Bound<'_, PyBytes>,
        key: Option<String>,
        idempotency_key: Option<String>,
        headers: Option<Vec<PyHeader>>,
        delay: Option<u64>,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<PyCallHandle> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .cmd_call(
                target(service, handler, key, idempotency_key, headers),
                buffer.as_bytes().to_vec().into(),
                invoke_at(delay),
                options,
            )
            .map(|handle| PyCallHandle {
                invocation_id_handle: handle.invocation_id_notification_handle.into(),
                result_handle: handle.call_notification_handle.into(),
            })
            .map_err(vm_error)
    }

    /// Returns the handle of the invocation id. `delay` is in milliseconds.
    #[pyo3(signature = (service, handler, buffer, key=None, delay=None, idempotency_key=None, headers=None, *, name=None, parent_span=None, record=None))]
    #[allow(clippy::too_many_arguments)]
    fn send(
        &mut self,
        service: String,
        handler: String,
        buffer: &Bound<'_, PyBytes>,
        key: Option<String>,
        delay: Option<u64>,
        idempotency_key: Option<String>,
        headers: Option<Vec<PyHeader>>,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<u32> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .cmd_send(
                target(service, handler, key, idempotency_key, headers),
                buffer.as_bytes().to_vec().into(),
                invoke_at(delay),
                options,
            )
            .map(|handle| handle.invocation_id_notification_handle.into())
            .map_err(vm_error)
    }

    #[pyo3(signature = (invocation_id, *, name=None, parent_span=None, record=None))]
    fn attach_invocation(
        &mut self,
        invocation_id: String,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<u32> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .cmd_attach_invocation(AttachInvocationTarget::InvocationId(invocation_id), options)
            .map(u32::from)
            .map_err(vm_error)
    }

    #[pyo3(signature = (invocation_id, *, want_result=false, name=None, parent_span=None, record=None))]
    fn cancel_invocation(
        &mut self,
        invocation_id: String,
        want_result: bool,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<Option<u32>> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .cmd_cancel_invocation(invocation_id, options, want_result)
            .map(handle_of)
            .map_err(vm_error)
    }

    // --- Signals

    /// The signal `index` (from 0) of the name `signal_name` this invocation received: its handle.
    #[pyo3(signature = (signal_name, index, *, name=None, parent_span=None, record=None))]
    fn signal_read(
        &mut self,
        signal_name: String,
        index: u32,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<u32> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .cmd_signal_read(signal_name, index, options)
            .map(u32::from)
            .map_err(vm_error)
    }

    /// `value`: `None` (a signal that carries nothing), `bytes`, or a `PyFailure`.
    #[pyo3(signature = (target_invocation_id, signal_name, value=None, *, want_result=false, name=None, parent_span=None, record=None))]
    #[allow(clippy::too_many_arguments)]
    fn signal_send(
        &mut self,
        target_invocation_id: String,
        signal_name: String,
        value: Option<&Bound<'_, PyAny>>,
        want_result: bool,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<Option<u32>> {
        let options = options_of(name, parent_span, record)?;
        let value = non_empty_value_of(value)?;
        self.vm
            .cmd_signal_send(
                target_invocation_id,
                signal_name,
                value,
                options,
                want_result,
            )
            .map(handle_of)
            .map_err(vm_error)
    }

    // --- Durability

    /// Completes (`PyVoid`) once every action made before it is applied.
    #[pyo3(signature = (*, name=None, parent_span=None, record=None))]
    fn flush(
        &mut self,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<u32> {
        let options = options_of(name, parent_span, record)?;
        self.vm.cmd_flush(options).map(u32::from).map_err(vm_error)
    }

    /// Opens an atomic group: the writes, calls, sends, cancels, signal sends and spans made until
    /// `atomic_group_end` leave as one action. Never across an await.
    fn atomic_group_start(&mut self) -> PyResult<()> {
        self.vm.atomic_group_start().map_err(vm_error)
    }

    #[pyo3(signature = (*, want_result=false, name=None, parent_span=None, record=None))]
    fn atomic_group_end(
        &mut self,
        want_result: bool,
        name: Option<String>,
        parent_span: Option<u32>,
        record: Option<&str>,
    ) -> PyResult<Option<u32>> {
        let options = options_of(name, parent_span, record)?;
        self.vm
            .atomic_group_end(options, want_result)
            .map(handle_of)
            .map_err(vm_error)
    }

    /// Drops the open group: returns the handles its actions returned, never notified.
    fn atomic_group_abort(&mut self) -> PyResult<Vec<u32>> {
        self.vm
            .atomic_group_abort()
            .map(|handles| handles.into_iter().map(u32::from).collect())
            .map_err(vm_error)
    }

    // --- Spans

    /// Inside the span `parent` of this attempt, or else inside the attempt itself.
    #[pyo3(signature = (name, parent=None))]
    fn span_start(&mut self, name: String, parent: Option<u32>) -> PyResult<u32> {
        let parent = parent.map(span_of).transpose()?;
        self.vm
            .span_start(name, parent)
            .map(u32::from)
            .map_err(vm_error)
    }

    /// `result`: `None` (nothing), `bytes`, or a `PyFailure`.
    #[pyo3(signature = (span, result=None))]
    fn span_end(&mut self, span: u32, result: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        let result = non_empty_value_of(result)?;
        self.vm.span_end(span_of(span)?, result).map_err(vm_error)
    }

    fn heartbeat(&mut self) -> PyResult<()> {
        self.vm.heartbeat().map_err(vm_error)
    }

    // --- End

    fn end_success(&mut self, buffer: &Bound<'_, PyBytes>) -> PyResult<()> {
        self.vm
            .end(NonEmptyValue::Success(buffer.as_bytes().to_vec().into()))
            .map_err(vm_error)
    }

    fn end_failure(&mut self, failure: PyFailure) -> PyResult<()> {
        self.vm
            .end(NonEmptyValue::Failure(failure.into()))
            .map_err(vm_error)
    }

    #[pyo3(signature = (error, stacktrace=None, delay_override_ms=None))]
    fn notify_error(
        &mut self,
        error: String,
        stacktrace: Option<String>,
        delay_override_ms: Option<u64>,
    ) {
        let mut error = Error::new(restate_sdk_shared_core::error::codes::INTERNAL, error);
        if let Some(stacktrace) = stacktrace {
            error = error.with_stacktrace(stacktrace);
        }
        if let Some(delay) = delay_override_ms {
            error = error.with_next_retry_delay_override(Duration::from_millis(delay));
        }
        self.vm.notify_error(error);
    }

    /// The SDK ends the attempt on its own (SIGTERM): an unasked `ShutdownMessage`.
    fn shutdown(&mut self) {
        self.vm.shutdown();
    }

    /// Whether the invocation was cancelled, as far as the handler was told: before this attempt,
    /// or by the cancel signal a tick took.
    fn is_cancelled(&self) -> bool {
        self.vm.is_cancelled()
    }

    /// `"starting"`, `"running"` or `"closed"`.
    fn state(&self) -> &'static str {
        match self.vm.state() {
            AttemptState::Starting => "starting",
            AttemptState::Running => "running",
            AttemptState::Closed => "closed",
        }
    }
}
