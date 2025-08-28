use pyo3::create_exception;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyNone, PyString};
use restate_sdk_shared_core::{
    CallHandle, CoreVM, DoProgressResponse, Error, Header, IdentityVerifier, Input, NonEmptyValue,
    NotificationHandle, ResponseHead, RetryPolicy, RunExitResult, TakeOutputResult, Target,
    TerminalFailure, VMOptions, Value, CANCEL_NOTIFICATION_HANDLE, VM,
};
use std::time::{Duration, SystemTime};

// Current crate version
const CURRENT_VERSION: &str = env!("CARGO_PKG_VERSION");

// Data model

#[pyclass]
#[derive(Clone)]
struct PyHeader {
    #[pyo3(get, set)]
    key: String,
    #[pyo3(get, set)]
    value: String,
}

#[pymethods]
impl PyHeader {
    #[new]
    fn new(key: String, value: String) -> PyHeader {
        Self { key, value }
    }
}

impl From<Header> for PyHeader {
    fn from(h: Header) -> Self {
        PyHeader {
            key: h.key.into(),
            value: h.value.into(),
        }
    }
}

impl From<PyHeader> for Header {
    fn from(h: PyHeader) -> Self {
        Header {
            key: h.key.into(),
            value: h.value.into(),
        }
    }
}

#[pyclass]
struct PyResponseHead {
    #[pyo3(get, set)]
    status_code: u16,
    #[pyo3(get, set)]
    headers: Vec<(String, String)>,
}

impl From<ResponseHead> for PyResponseHead {
    fn from(value: ResponseHead) -> Self {
        PyResponseHead {
            status_code: value.status_code,
            headers: value
                .headers
                .into_iter()
                .map(|Header { key, value }| (key.into(), value.into()))
                .collect(),
        }
    }
}

fn take_output_result_into_py(
    py: Python,
    take_output_result: TakeOutputResult,
) -> Bound<'_, PyAny> {
    match take_output_result {
        TakeOutputResult::Buffer(b) => PyBytes::new(py, &b).into_any(),
        TakeOutputResult::EOF => PyNone::get(py).to_owned().into_any(),
    }
}

type PyNotificationHandle = u32;

#[pyclass]
struct PyVoid;

#[pyclass]
struct PySuspended;

#[pyclass]
#[derive(Clone)]
struct PyFailure {
    #[pyo3(get, set)]
    code: u16,
    #[pyo3(get, set)]
    message: String,
}

#[pymethods]
impl PyFailure {
    #[new]
    fn new(code: u16, message: String) -> PyFailure {
        Self { code, message }
    }
}

#[pyclass]
#[derive(Clone)]
struct PyExponentialRetryConfig {
    #[pyo3(get, set)]
    initial_interval: Option<u64>,
    #[pyo3(get, set)]
    max_attempts: Option<u32>,
    #[pyo3(get, set)]
    max_duration: Option<u64>,
}

#[pymethods]
impl PyExponentialRetryConfig {
    #[pyo3(signature = (initial_interval=None, max_attempts=None, max_duration=None))]
    #[new]
    fn new(
        initial_interval: Option<u64>,
        max_attempts: Option<u32>,
        max_duration: Option<u64>,
    ) -> Self {
        Self {
            initial_interval,
            max_attempts,
            max_duration,
        }
    }
}

impl From<PyExponentialRetryConfig> for RetryPolicy {
    fn from(value: PyExponentialRetryConfig) -> Self {
        RetryPolicy::Exponential {
            initial_interval: Duration::from_millis(value.initial_interval.unwrap_or(10)),
            max_attempts: value.max_attempts,
            max_duration: value.max_duration.map(Duration::from_millis),
            factor: 2.0,
            max_interval: None,
        }
    }
}

impl From<TerminalFailure> for PyFailure {
    fn from(value: TerminalFailure) -> Self {
        PyFailure {
            code: value.code,
            message: value.message,
        }
    }
}

impl From<PyFailure> for TerminalFailure {
    fn from(value: PyFailure) -> Self {
        TerminalFailure {
            code: value.code,
            message: value.message,
        }
    }
}

impl From<PyFailure> for Error {
    fn from(value: PyFailure) -> Self {
        Self::new(value.code, value.message)
    }
}

#[pyclass]
#[derive(Clone)]
struct PyStateKeys {
    #[pyo3(get, set)]
    keys: Vec<String>,
}

#[pyclass]
pub struct PyInput {
    #[pyo3(get, set)]
    invocation_id: String,
    #[pyo3(get, set)]
    random_seed: u64,
    #[pyo3(get, set)]
    key: String,
    #[pyo3(get, set)]
    headers: Vec<PyHeader>,
    #[pyo3(get, set)]
    input: Vec<u8>,
}

impl From<Input> for PyInput {
    fn from(value: Input) -> Self {
        PyInput {
            invocation_id: value.invocation_id,
            random_seed: value.random_seed,
            key: value.key,
            headers: value.headers.into_iter().map(Into::into).collect(),
            input: value.input.into(),
        }
    }
}

#[pyclass]
struct PyDoProgressReadFromInput;

#[pyclass]
struct PyDoProgressAnyCompleted;

#[pyclass]
struct PyDoProgressExecuteRun {
    #[pyo3(get)]
    handle: PyNotificationHandle,
}

#[pyclass]
struct PyDoProgressCancelSignalReceived;

#[pyclass]
struct PyDoWaitForPendingRun;

#[pyclass]
pub struct PyCallHandle {
    #[pyo3(get)]
    invocation_id_handle: PyNotificationHandle,
    #[pyo3(get)]
    result_handle: PyNotificationHandle,
}

impl From<CallHandle> for PyCallHandle {
    fn from(value: CallHandle) -> Self {
        PyCallHandle {
            invocation_id_handle: value.invocation_id_notification_handle.into(),
            result_handle: value.call_notification_handle.into(),
        }
    }
}

// Errors and Exceptions

#[derive(Debug)]
struct PyVMError(Error);

// Python representation of restate_sdk_shared_core::Error
create_exception!(
    restate_sdk_python_core,
    VMException,
    pyo3::exceptions::PyException,
    "Restate VM exception."
);

impl From<PyVMError> for PyErr {
    fn from(value: PyVMError) -> Self {
        VMException::new_err(value.0.to_string())
    }
}

impl From<Error> for PyVMError {
    fn from(value: Error) -> Self {
        PyVMError(value)
    }
}

// VM implementation

#[pyclass]
struct PyVM {
    vm: CoreVM,
}

#[pymethods]
impl PyVM {
    #[new]
    fn new(headers: Vec<(String, String)>) -> Result<Self, PyVMError> {
        Ok(Self {
            vm: CoreVM::new(headers, VMOptions::default())?,
        })
    }

    fn get_response_head(self_: PyRef<'_, Self>) -> PyResponseHead {
        self_.vm.get_response_head().into()
    }

    // Notifications

    fn notify_input(mut self_: PyRefMut<'_, Self>, buffer: &Bound<'_, PyBytes>) {
        let buf = buffer.as_bytes().to_vec().into();
        self_.vm.notify_input(buf);
    }

    fn notify_input_closed(mut self_: PyRefMut<'_, Self>) {
        self_.vm.notify_input_closed();
    }

    #[pyo3(signature = (error, stacktrace=None))]
    fn notify_error(mut self_: PyRefMut<'_, Self>, error: String, stacktrace: Option<String>) {
        let mut error = Error::new(restate_sdk_shared_core::error::codes::INTERNAL, error);
        if let Some(desc) = stacktrace {
            error = error.with_stacktrace(desc);
        }
        CoreVM::notify_error(&mut self_.vm, error, None);
    }

    // Take(s)

    /// Returns either bytes or None, indicating EOF
    fn take_output(mut self_: PyRefMut<'_, Self>) -> Bound<'_, PyAny> {
        take_output_result_into_py(self_.py(), self_.vm.take_output())
    }

    fn is_ready_to_execute(self_: PyRef<'_, Self>) -> Result<bool, PyVMError> {
        self_.vm.is_ready_to_execute().map_err(Into::into)
    }

    fn is_completed(self_: PyRef<'_, Self>, handle: PyNotificationHandle) -> bool {
        self_.vm.is_completed(handle.into())
    }

    fn do_progress(
        mut self_: PyRefMut<'_, Self>,
        any_handle: Vec<PyNotificationHandle>,
    ) -> PyResult<Bound<'_, PyAny>> {
        let res = self_.vm.do_progress(
            any_handle
                .into_iter()
                .map(NotificationHandle::from)
                .collect(),
        );

        let py = self_.py();

        match res {
            Err(e) if e.is_suspended_error() => Ok(Bound::new(py, PySuspended)?.into_any()),
            Err(e) => Err(PyVMError::from(e))?,
            Ok(DoProgressResponse::AnyCompleted) => {
                Ok(Bound::new(py, PyDoProgressAnyCompleted)?.into_any())
            }
            Ok(DoProgressResponse::ReadFromInput) => {
                Ok(Bound::new(py, PyDoProgressReadFromInput)?.into_any())
            }
            Ok(DoProgressResponse::ExecuteRun(handle)) => Ok(Bound::new(
                py,
                PyDoProgressExecuteRun {
                    handle: handle.into(),
                },
            )?
            .into_any()),
            Ok(DoProgressResponse::CancelSignalReceived) => {
                Ok(Bound::new(py, PyDoProgressCancelSignalReceived)?.into_any())
            }
            Ok(DoProgressResponse::WaitingPendingRun) => {
                Ok(Bound::new(py, PyDoWaitForPendingRun)?.into_any())
            }
        }
    }

    /// Returns either:
    ///
    /// * `PyBytes` in case the async result holds success value
    /// * `PyFailure` in case the async result holds failure value
    /// * `PyVoid` in case the async result holds Void value
    /// * `PyStateKeys` in case the async result holds StateKeys
    /// * `PyString` in case the async result holds invocation id
    /// * `PySuspended` in case the state machine is suspended
    /// * `None` in case the async result is not yet present
    fn take_notification(
        mut self_: PyRefMut<'_, Self>,
        handle: PyNotificationHandle,
    ) -> PyResult<Bound<'_, PyAny>> {
        let res = self_.vm.take_notification(NotificationHandle::from(handle));

        let py = self_.py();

        match res {
            Err(e) if e.is_suspended_error() => Ok(Bound::new(py, PySuspended)?.into_any()),
            Err(e) => Err(PyVMError::from(e))?,
            Ok(None) => Ok(PyNone::get(py).to_owned().into_any()),
            Ok(Some(Value::Void)) => Ok(Bound::new(py, PyVoid)?.into_any()),
            Ok(Some(Value::Success(b))) => Ok(PyBytes::new(py, &b).into_any()),
            Ok(Some(Value::Failure(f))) => Ok(Bound::new(py, PyFailure::from(f))?.into_any()),
            Ok(Some(Value::StateKeys(keys))) => {
                Ok(Bound::new(py, PyStateKeys { keys })?.into_any())
            }
            Ok(Some(Value::InvocationId(invocation_id))) => {
                Ok(PyString::new(py, &invocation_id).into_any())
            }
        }
    }

    // Syscall(s)

    fn sys_input(mut self_: PyRefMut<'_, Self>) -> Result<PyInput, PyVMError> {
        self_.vm.sys_input().map(Into::into).map_err(Into::into)
    }

    fn sys_get_state(
        mut self_: PyRefMut<'_, Self>,
        key: String,
    ) -> Result<PyNotificationHandle, PyVMError> {
        self_
            .vm
            .sys_state_get(key)
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_get_state_keys(
        mut self_: PyRefMut<'_, Self>,
    ) -> Result<PyNotificationHandle, PyVMError> {
        self_
            .vm
            .sys_state_get_keys()
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_set_state(
        mut self_: PyRefMut<'_, Self>,
        key: String,
        buffer: &Bound<'_, PyBytes>,
    ) -> Result<(), PyVMError> {
        self_
            .vm
            .sys_state_set(key, buffer.as_bytes().to_vec().into())
            .map_err(Into::into)
    }

    fn sys_clear_state(mut self_: PyRefMut<'_, Self>, key: String) -> Result<(), PyVMError> {
        self_.vm.sys_state_clear(key).map_err(Into::into)
    }

    fn sys_clear_all_state(mut self_: PyRefMut<'_, Self>) -> Result<(), PyVMError> {
        self_.vm.sys_state_clear_all().map_err(Into::into)
    }

    fn sys_sleep(
        mut self_: PyRefMut<'_, Self>,
        millis: u64,
    ) -> Result<PyNotificationHandle, PyVMError> {
        let now = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .expect("Duration since unix epoch cannot fail");
        self_
            .vm
            .sys_sleep(
                String::default(),
                now + Duration::from_millis(millis),
                Some(now),
            )
            .map(Into::into)
            .map_err(Into::into)
    }

    #[pyo3(signature = (service, handler, buffer, key=None, idempotency_key=None, headers=None))]
    fn sys_call(
        mut self_: PyRefMut<'_, Self>,
        service: String,
        handler: String,
        buffer: &Bound<'_, PyBytes>,
        key: Option<String>,
        idempotency_key: Option<String>,
        headers: Option<Vec<PyHeader>>,
    ) -> Result<PyCallHandle, PyVMError> {
        self_
            .vm
            .sys_call(
                Target {
                    service,
                    handler,
                    key,
                    idempotency_key,
                    headers: headers
                        .unwrap_or_default()
                        .into_iter()
                        .map(Into::into)
                        .collect(),
                },
                buffer.as_bytes().to_vec().into(),
            )
            .map(Into::into)
            .map_err(Into::into)
    }

    #[pyo3(signature = (service, handler, buffer, key=None, delay=None, idempotency_key=None, headers=None))]
    #[allow(clippy::too_many_arguments)]
    fn sys_send(
        mut self_: PyRefMut<'_, Self>,
        service: String,
        handler: String,
        buffer: &Bound<'_, PyBytes>,
        key: Option<String>,
        delay: Option<u64>,
        idempotency_key: Option<String>,
        headers: Option<Vec<PyHeader>>,
    ) -> Result<PyNotificationHandle, PyVMError> {
        self_
            .vm
            .sys_send(
                Target {
                    service,
                    handler,
                    key,
                    idempotency_key,
                    headers: headers
                        .unwrap_or_default()
                        .into_iter()
                        .map(Into::into)
                        .collect(),
                },
                buffer.as_bytes().to_vec().into(),
                delay.map(|millis| {
                    SystemTime::now()
                        .duration_since(SystemTime::UNIX_EPOCH)
                        .expect("Duration since unix epoch cannot fail")
                        + Duration::from_millis(millis)
                }),
            )
            .map(|s| s.invocation_id_notification_handle.into())
            .map_err(Into::into)
    }

    fn sys_awakeable(
        mut self_: PyRefMut<'_, Self>,
    ) -> Result<(String, PyNotificationHandle), PyVMError> {
        self_
            .vm
            .sys_awakeable()
            .map(|(id, handle)| (id, handle.into()))
            .map_err(Into::into)
    }

    fn sys_complete_awakeable_success(
        mut self_: PyRefMut<'_, Self>,
        id: String,
        buffer: &Bound<'_, PyBytes>,
    ) -> Result<(), PyVMError> {
        self_
            .vm
            .sys_complete_awakeable(
                id,
                NonEmptyValue::Success(buffer.as_bytes().to_vec().into()),
            )
            .map_err(Into::into)
    }

    fn sys_complete_awakeable_failure(
        mut self_: PyRefMut<'_, Self>,
        id: String,
        value: PyFailure,
    ) -> Result<(), PyVMError> {
        self_
            .vm
            .sys_complete_awakeable(id, NonEmptyValue::Failure(value.into()))
            .map_err(Into::into)
    }

    fn sys_get_promise(
        mut self_: PyRefMut<'_, Self>,
        key: String,
    ) -> Result<PyNotificationHandle, PyVMError> {
        self_
            .vm
            .sys_get_promise(key)
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_peek_promise(
        mut self_: PyRefMut<'_, Self>,
        key: String,
    ) -> Result<PyNotificationHandle, PyVMError> {
        self_
            .vm
            .sys_peek_promise(key)
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_complete_promise_success(
        mut self_: PyRefMut<'_, Self>,
        key: String,
        buffer: &Bound<'_, PyBytes>,
    ) -> Result<PyNotificationHandle, PyVMError> {
        self_
            .vm
            .sys_complete_promise(
                key,
                NonEmptyValue::Success(buffer.as_bytes().to_vec().into()),
            )
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_complete_promise_failure(
        mut self_: PyRefMut<'_, Self>,
        key: String,
        value: PyFailure,
    ) -> Result<PyNotificationHandle, PyVMError> {
        self_
            .vm
            .sys_complete_promise(key, NonEmptyValue::Failure(value.into()))
            .map(Into::into)
            .map_err(Into::into)
    }

    /// Returns the associated `PyNotificationHandle`.
    fn sys_run(
        mut self_: PyRefMut<'_, Self>,
        name: String,
    ) -> Result<PyNotificationHandle, PyVMError> {
        self_.vm.sys_run(name).map(Into::into).map_err(Into::into)
    }

    fn sys_cancel(mut self_: PyRefMut<'_, Self>, invocation_id: String) -> Result<(), PyVMError> {
        self_
            .vm
            .sys_cancel_invocation(invocation_id)
            .map_err(Into::into)
    }

    fn propose_run_completion_success(
        mut self_: PyRefMut<'_, Self>,
        handle: PyNotificationHandle,
        buffer: &Bound<'_, PyBytes>,
    ) -> Result<(), PyVMError> {
        CoreVM::propose_run_completion(
            &mut self_.vm,
            handle.into(),
            RunExitResult::Success(buffer.as_bytes().to_vec().into()),
            RetryPolicy::None,
        )
        .map_err(Into::into)
    }

    fn propose_run_completion_failure(
        mut self_: PyRefMut<'_, Self>,
        handle: PyNotificationHandle,
        value: PyFailure,
    ) -> Result<(), PyVMError> {
        self_
            .vm
            .propose_run_completion(
                handle.into(),
                RunExitResult::TerminalFailure(value.into()),
                RetryPolicy::None,
            )
            .map_err(Into::into)
    }

    fn propose_run_completion_failure_transient(
        mut self_: PyRefMut<'_, Self>,
        handle: PyNotificationHandle,
        value: PyFailure,
        attempt_duration: u64,
        config: PyExponentialRetryConfig,
    ) -> Result<(), PyVMError> {
        self_
            .vm
            .propose_run_completion(
                handle.into(),
                RunExitResult::RetryableFailure {
                    attempt_duration: Duration::from_millis(attempt_duration),
                    error: value.into(),
                },
                config.into(),
            )
            .map_err(Into::into)
    }

    fn sys_write_output_success(
        mut self_: PyRefMut<'_, Self>,
        buffer: &Bound<'_, PyBytes>,
    ) -> Result<(), PyVMError> {
        self_
            .vm
            .sys_write_output(NonEmptyValue::Success(buffer.as_bytes().to_vec().into()))
            .map_err(Into::into)
    }

    fn sys_write_output_failure(
        mut self_: PyRefMut<'_, Self>,
        value: PyFailure,
    ) -> Result<(), PyVMError> {
        self_
            .vm
            .sys_write_output(NonEmptyValue::Failure(value.into()))
            .map_err(Into::into)
    }

    fn attach_invocation(
        mut self_: PyRefMut<'_, Self>,
        invocation_id: String,
    ) -> Result<PyNotificationHandle, PyVMError> {
        self_
            .vm
            .sys_attach_invocation(
                restate_sdk_shared_core::AttachInvocationTarget::InvocationId(invocation_id),
            )
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_end(mut self_: PyRefMut<'_, Self>) -> Result<(), PyVMError> {
        self_.vm.sys_end().map_err(Into::into)
    }

    fn is_replaying(self_: PyRef<'_, Self>) -> bool {
        self_.vm.is_replaying()
    }
}

#[pyclass]
struct PyIdentityVerifier {
    verifier: IdentityVerifier,
}

// Exceptions
create_exception!(
    restate_sdk_python_core,
    IdentityKeyException,
    pyo3::exceptions::PyException,
    "Restate identity key exception."
);

create_exception!(
    restate_sdk_python_core,
    IdentityVerificationException,
    pyo3::exceptions::PyException,
    "Restate identity verification exception."
);

#[pymethods]
impl PyIdentityVerifier {
    #[new]
    fn new(keys: Vec<String>) -> PyResult<Self> {
        Ok(Self {
            verifier: IdentityVerifier::new(&keys.iter().map(|x| &**x).collect::<Vec<_>>())
                .map_err(|e| IdentityKeyException::new_err(e.to_string()))?,
        })
    }

    fn verify(
        self_: PyRef<'_, Self>,
        headers: Vec<(String, String)>,
        path: String,
    ) -> PyResult<()> {
        self_
            .verifier
            .verify_identity(&headers, &path)
            .map_err(|e| IdentityVerificationException::new_err(e.to_string()))
    }
}

#[pymodule]
fn _internal(m: &Bound<'_, PyModule>) -> PyResult<()> {
    use tracing_subscriber::EnvFilter;

    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_env("RESTATE_CORE_LOG"))
        .init();

    m.add_class::<PyHeader>()?;
    m.add_class::<PyResponseHead>()?;
    m.add_class::<PyFailure>()?;
    m.add_class::<PyInput>()?;
    m.add_class::<PyVoid>()?;
    m.add_class::<PyStateKeys>()?;
    m.add_class::<PySuspended>()?;
    m.add_class::<PyVM>()?;
    m.add_class::<PyIdentityVerifier>()?;
    m.add_class::<PyExponentialRetryConfig>()?;
    m.add_class::<PyDoProgressAnyCompleted>()?;
    m.add_class::<PyDoProgressReadFromInput>()?;
    m.add_class::<PyDoProgressExecuteRun>()?;
    m.add_class::<PyDoProgressCancelSignalReceived>()?;
    m.add_class::<PyDoWaitForPendingRun>()?;
    m.add_class::<PyCallHandle>()?;

    m.add("VMException", m.py().get_type::<VMException>())?;
    m.add(
        "IdentityKeyException",
        m.py().get_type::<IdentityKeyException>(),
    )?;
    m.add(
        "IdentityVerificationException",
        m.py().get_type::<IdentityVerificationException>(),
    )?;
    m.add("SDK_VERSION", CURRENT_VERSION)?;
    m.add(
        "CANCEL_NOTIFICATION_HANDLE",
        PyNotificationHandle::from(CANCEL_NOTIFICATION_HANDLE),
    )?;
    Ok(())
}
