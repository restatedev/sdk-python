//! The VM of a session object's attempt: shared-core's [`CoreExplicitRecoveryVM`], which never
//! replays a journal.
//!
//! An action returns a handle, and its result comes back as a notification of that handle
//! (`take_notification`): `restate/sessions/_driver.py` settles its futures with them. The
//! database's store ([`crate::sessions`]) awaits its own handles from Rust: their completions go
//! to it ([`PyExplicitRecoveryVM::wait_in_rust`]), never to Python.
use crate::{
    PyCallHandle, PyFailure, PyHeader, PyInput, PyResponseHead, PyStateKeys, PyVoid, VMException,
};
use futures::channel::oneshot;
use pyo3::create_exception;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};
use restate_sdk_shared_core::explicit_recovery::{
    request_response_unsupported, AttemptState, CoreExplicitRecoveryVM, ExplicitRecoveryVM,
    Notification, TraceId,
};
use restate_sdk_shared_core::{
    AttachInvocationTarget, Error, NonEmptyValue, NotificationHandle, Target, Value,
};
use std::collections::{HashMap, VecDeque};
use std::time::{Duration, SystemTime};

create_exception!(
    restate_sdk_python_core,
    AttemptShutDownException,
    pyo3::exceptions::PyException,
    "Restate shut the attempt down: it is over, and a new attempt will run the handler from the start."
);

/// A VM error as Python gets it: the attempt's shutdown is an `AttemptShutDownException`.
pub(crate) fn vm_error(error: Error) -> PyErr {
    if error.is_suspended_error() {
        AttemptShutDownException::new_err(error.to_string())
    } else {
        VMException::new_err(error.to_string())
    }
}

/// Where Rust waits for the notification of a handle.
type RustWaiter = oneshot::Sender<Result<Value, Error>>;

#[pyclass]
pub(crate) struct PyExplicitRecoveryVM {
    pub(crate) vm: CoreExplicitRecoveryVM,
    /// The handles Rust waits for (the database's reads and acks), with their waiter.
    rust_waiters: HashMap<NotificationHandle, RustWaiter>,
    /// The completions taken from the VM for Python, not yet taken by it.
    for_python: VecDeque<Notification>,
    /// What Rust calls after its own calls to the VM: it schedules Python's tick, which writes
    /// the output and settles Python's futures.
    scheduler: Option<Py<PyAny>>,
}

impl PyExplicitRecoveryVM {
    /// Take the VM's completions, handing Rust's to their waiters. Once the attempt closed with
    /// an error, every waiter gets it.
    fn route(&mut self) -> Result<(), Error> {
        loop {
            match self.vm.take_next_notification() {
                Ok(Some(notification)) => match self.rust_waiters.remove(&notification.handle) {
                    Some(waiter) => {
                        let _ = waiter.send(Ok(notification.value));
                    }
                    None => self.for_python.push_back(notification),
                },
                Ok(None) => return Ok(()),
                Err(error) => {
                    for (_, waiter) in self.rust_waiters.drain() {
                        let _ = waiter.send(Err(error.clone()));
                    }
                    return Err(error);
                }
            }
        }
    }

    /// The notification of `handle`, for Rust. A handle already notified is at once.
    pub(crate) fn wait_in_rust(
        &mut self,
        handle: NotificationHandle,
    ) -> oneshot::Receiver<Result<Value, Error>> {
        let (sender, receiver) = oneshot::channel();
        self.rust_waiters.insert(handle, sender);
        // The error, if any, reaches the waiter.
        let _ = self.route();
        receiver
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
fn value_to_py(py: Python<'_>, value: Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::Void => Py::new(py, PyVoid)?.into_any(),
        Value::Success(b) => PyBytes::new(py, &b).into_any().unbind(),
        Value::Failure(f) => Py::new(py, PyFailure::from(f))?.into_any(),
        Value::StateKeys(keys) => Py::new(py, PyStateKeys { keys })?.into_any(),
        Value::InvocationId(id) => PyString::new(py, &id).into_any().unbind(),
    })
}

#[pymethods]
impl PyExplicitRecoveryVM {
    /// Without a `bidirectional` stream (a request/response transport, e.g. AWS Lambda), the VM
    /// fails the invocation at once, pausing it: no response could reach the handler.
    #[new]
    fn new(headers: Vec<(String, String)>, bidirectional: bool) -> PyResult<Self> {
        let mut vm = CoreExplicitRecoveryVM::new(headers).map_err(vm_error)?;
        if !bidirectional {
            vm.notify_error(request_response_unsupported());
        }
        Ok(Self {
            vm,
            rust_waiters: Default::default(),
            for_python: Default::default(),
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

    fn is_ready_to_execute(&self) -> PyResult<bool> {
        self.vm.is_ready_to_execute().map_err(vm_error)
    }

    fn input(&mut self) -> PyResult<PyInput> {
        self.vm.get_input().map(Into::into).map_err(vm_error)
    }

    /// The next completion for Python, `(handle, value)`, or `None`. Raises once the attempt is
    /// closed by a shutdown (`AttemptShutDownException`) or a failure (`VMException`).
    fn take_notification(&mut self, py: Python<'_>) -> PyResult<Option<(u32, Py<PyAny>)>> {
        self.route().map_err(vm_error)?;
        match self.for_python.pop_front() {
            Some(Notification { handle, value }) => {
                Ok(Some((u32::from(handle), value_to_py(py, value)?)))
            }
            None => Ok(None),
        }
    }

    // --- Request/response actions

    #[pyo3(signature = (key, name=None))]
    fn get_state(&mut self, key: String, name: Option<String>) -> PyResult<u32> {
        self.vm
            .cmd_state_get(key, name)
            .map(u32::from)
            .map_err(vm_error)
    }

    #[pyo3(signature = (name=None))]
    fn get_state_keys(&mut self, name: Option<String>) -> PyResult<u32> {
        self.vm
            .cmd_state_get_keys(name)
            .map(u32::from)
            .map_err(vm_error)
    }

    #[pyo3(signature = (service, handler, buffer, key=None, idempotency_key=None, headers=None, name=None))]
    #[allow(clippy::too_many_arguments)]
    fn call(
        &mut self,
        service: String,
        handler: String,
        buffer: &Bound<'_, PyBytes>,
        key: Option<String>,
        idempotency_key: Option<String>,
        headers: Option<Vec<PyHeader>>,
        name: Option<String>,
    ) -> PyResult<PyCallHandle> {
        self.vm
            .cmd_call(
                target(service, handler, key, idempotency_key, headers),
                buffer.as_bytes().to_vec().into(),
                name,
            )
            .map(|handle| PyCallHandle {
                invocation_id_handle: handle.invocation_id_notification_handle.into(),
                result_handle: handle.call_notification_handle.into(),
            })
            .map_err(vm_error)
    }

    #[pyo3(signature = (invocation_id, name=None))]
    fn attach_invocation(&mut self, invocation_id: String, name: Option<String>) -> PyResult<u32> {
        self.vm
            .cmd_attach_invocation(AttachInvocationTarget::InvocationId(invocation_id), name)
            .map(u32::from)
            .map_err(vm_error)
    }

    // --- Fire-and-forget actions

    #[pyo3(signature = (key, buffer, name=None, ack=false))]
    fn set_state(
        &mut self,
        key: String,
        buffer: &Bound<'_, PyBytes>,
        name: Option<String>,
        ack: bool,
    ) -> PyResult<u32> {
        self.vm
            .cmd_state_set(key, buffer.as_bytes().to_vec().into(), name, ack)
            .map(u32::from)
            .map_err(vm_error)
    }

    #[pyo3(signature = (key, name=None, ack=false))]
    fn clear_state(&mut self, key: String, name: Option<String>, ack: bool) -> PyResult<u32> {
        self.vm
            .cmd_state_clear(key, name, ack)
            .map(u32::from)
            .map_err(vm_error)
    }

    #[pyo3(signature = (name=None, ack=false))]
    fn clear_all_state(&mut self, name: Option<String>, ack: bool) -> PyResult<u32> {
        self.vm
            .cmd_state_clear_all(name, ack)
            .map(u32::from)
            .map_err(vm_error)
    }

    #[pyo3(signature = (invocation_id, name=None, ack=false))]
    fn cancel_invocation(
        &mut self,
        invocation_id: String,
        name: Option<String>,
        ack: bool,
    ) -> PyResult<u32> {
        self.vm
            .cmd_cancel_invocation(invocation_id, name, ack)
            .map(u32::from)
            .map_err(vm_error)
    }

    /// Returns the handle of the invocation id. `delay` is in milliseconds.
    #[pyo3(signature = (service, handler, buffer, key=None, delay=None, idempotency_key=None, headers=None, name=None))]
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
    ) -> PyResult<u32> {
        let execute_at = delay.map(|millis| {
            SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .expect("Duration since unix epoch cannot fail")
                + Duration::from_millis(millis)
        });
        self.vm
            .cmd_send(
                target(service, handler, key, idempotency_key, headers),
                buffer.as_bytes().to_vec().into(),
                execute_at,
                name,
            )
            .map(|handle| handle.invocation_id_notification_handle.into())
            .map_err(vm_error)
    }

    /// Inside the trace `parent` of this attempt, or else inside the attempt itself.
    #[pyo3(signature = (name, parent=None))]
    fn start_trace(&mut self, name: String, parent: Option<u32>) -> PyResult<u32> {
        self.vm
            .trace_start(name, parent.map(TraceId::from))
            .map(u32::from)
            .map_err(vm_error)
    }

    fn end_trace_success(&mut self, trace: u32, buffer: &Bound<'_, PyBytes>) -> PyResult<()> {
        self.vm
            .trace_end(
                TraceId::from(trace),
                NonEmptyValue::Success(buffer.as_bytes().to_vec().into()),
            )
            .map_err(vm_error)
    }

    fn end_trace_failure(&mut self, trace: u32, failure: PyFailure) -> PyResult<()> {
        self.vm
            .trace_end(TraceId::from(trace), NonEmptyValue::Failure(failure.into()))
            .map_err(vm_error)
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

    /// Whether the invocation was cancelled, in this attempt or an earlier one.
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
