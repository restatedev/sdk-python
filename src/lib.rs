use pyo3::create_exception;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyNone};
use restate_sdk_shared_core::{
    AsyncResultHandle, CoreVM, Failure, Header, Input, NonEmptyValue, ResponseHead, RunEnterResult,
    SuspendedOrVMError, TakeOutputResult, Target, VMError, Value, VM,
};
use std::borrow::Cow;
use std::time::Duration;

// Data model

#[pyclass]
#[derive(Clone)]
struct PyHeader {
    #[pyo3(get, set)]
    key: String,
    #[pyo3(get, set)]
    value: String,
}

impl From<Header> for PyHeader {
    fn from(h: Header) -> Self {
        PyHeader {
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
        TakeOutputResult::Buffer(b) => PyBytes::new_bound(py, &b).into_any(),
        TakeOutputResult::EOF => PyNone::get_bound(py).to_owned().into_any(),
    }
}

type PyAsyncResultHandle = u32;

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

impl From<Failure> for PyFailure {
    fn from(value: Failure) -> Self {
        PyFailure {
            code: value.code,
            message: value.message,
        }
    }
}

impl From<PyFailure> for Failure {
    fn from(value: PyFailure) -> Self {
        Failure {
            code: value.code,
            message: value.message,
        }
    }
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
            input: value.input,
        }
    }
}

// Errors and Exceptions

#[derive(Debug)]
struct PyVMError(VMError);

// Python representation of VMError
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

impl From<VMError> for PyVMError {
    fn from(value: VMError) -> Self {
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
            vm: CoreVM::new(headers)?,
        })
    }

    fn get_response_head(self_: PyRef<'_, Self>) -> PyResponseHead {
        self_.vm.get_response_head().into()
    }

    // Notifications

    fn notify_input(mut self_: PyRefMut<'_, Self>, buffer: &Bound<'_, PyBytes>) {
        self_.vm.notify_input(buffer.as_bytes().to_vec());
    }

    fn notify_input_closed(mut self_: PyRefMut<'_, Self>) {
        self_.vm.notify_input_closed();
    }

    #[pyo3(signature = (error, description=None))]
    fn notify_error(mut self_: PyRefMut<'_, Self>, error: String, description: Option<String>) {
        self_.vm.notify_error(
            Cow::Owned(error),
            description.map(Cow::Owned).unwrap_or(Cow::Borrowed("")),
        );
    }

    // Take(s)

    /// Returns either bytes or None, indicating EOF
    fn take_output(mut self_: PyRefMut<'_, Self>) -> Bound<'_, PyAny> {
        take_output_result_into_py(self_.py(), self_.vm.take_output())
    }

    fn is_ready_to_execute(self_: PyRef<'_, Self>) -> Result<bool, PyVMError> {
        self_.vm.is_ready_to_execute().map_err(Into::into)
    }

    fn notify_await_point(mut self_: PyRefMut<'_, Self>, handle: PyAsyncResultHandle) {
        self_.vm.notify_await_point(handle.into())
    }

    /// Returns either:
    ///
    /// * `PyBytes` in case the async result holds success value
    /// * `PyFailure` in case the async result holds failure value
    /// * `PyVoid` in case the async result holds Void value
    /// * `PySuspended` in case the state machine is suspended
    /// * `None` in case the async result is not yet present
    fn take_async_result(
        mut self_: PyRefMut<'_, Self>,
        handle: PyAsyncResultHandle,
    ) -> Result<Bound<'_, PyAny>, PyVMError> {
        let res = self_.vm.take_async_result(AsyncResultHandle::from(handle));

        let py = self_.py();

        match res {
            Err(SuspendedOrVMError::VM(e)) => Err(e.into()),
            Err(SuspendedOrVMError::Suspended(_)) => {
                Ok(PySuspended.into_py(py).into_bound(py).into_any())
            }
            Ok(None) => Ok(PyNone::get_bound(py).to_owned().into_any()),
            Ok(Some(Value::Void)) => Ok(PyVoid.into_py(py).into_bound(py).into_any()),
            Ok(Some(Value::Success(b))) => Ok(PyBytes::new_bound(py, &b).into_any()),
            Ok(Some(Value::Failure(f))) => {
                Ok(PyFailure::from(f).into_py(py).into_bound(py).into_any())
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
    ) -> Result<PyAsyncResultHandle, PyVMError> {
        self_
            .vm
            .sys_get_state(key)
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
            .sys_set_state(key, buffer.as_bytes().to_vec())
            .map_err(Into::into)
    }

    fn sys_clear_state(mut self_: PyRefMut<'_, Self>, key: String) -> Result<(), PyVMError> {
        self_.vm.sys_clear_state(key).map_err(Into::into)
    }

    fn sys_clear_all_state(mut self_: PyRefMut<'_, Self>) -> Result<(), PyVMError> {
        self_.vm.sys_clear_all_state().map_err(Into::into)
    }

    fn sys_sleep(
        mut self_: PyRefMut<'_, Self>,
        millis: u64,
    ) -> Result<PyAsyncResultHandle, PyVMError> {
        self_
            .vm
            .sys_sleep(Duration::from_millis(millis))
            .map(Into::into)
            .map_err(Into::into)
    }

    #[pyo3(signature = (service, handler, buffer, key=None))]
    fn sys_call(
        mut self_: PyRefMut<'_, Self>,
        service: String,
        handler: String,
        buffer: &Bound<'_, PyBytes>,
        key: Option<String>,
    ) -> Result<PyAsyncResultHandle, PyVMError> {
        self_
            .vm
            .sys_call(
                Target {
                    service,
                    handler,
                    key,
                },
                buffer.as_bytes().to_vec(),
            )
            .map(Into::into)
            .map_err(Into::into)
    }

    #[pyo3(signature = (service, handler, buffer, key=None, delay=None))]
    fn sys_send(
        mut self_: PyRefMut<'_, Self>,
        service: String,
        handler: String,
        buffer: &Bound<'_, PyBytes>,
        key: Option<String>,
        delay: Option<u64>,
    ) -> Result<(), PyVMError> {
        self_
            .vm
            .sys_send(
                Target {
                    service,
                    handler,
                    key,
                },
                buffer.as_bytes().to_vec(),
                delay.map(Duration::from_millis),
            )
            .map_err(Into::into)
    }

    fn sys_awakeable(
        mut self_: PyRefMut<'_, Self>,
    ) -> Result<(String, PyAsyncResultHandle), PyVMError> {
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
            .sys_complete_awakeable(id, NonEmptyValue::Success(buffer.as_bytes().to_vec()))
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
    ) -> Result<PyAsyncResultHandle, PyVMError> {
        self_
            .vm
            .sys_get_promise(key)
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_peek_promise(
        mut self_: PyRefMut<'_, Self>,
        key: String,
    ) -> Result<PyAsyncResultHandle, PyVMError> {
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
    ) -> Result<PyAsyncResultHandle, PyVMError> {
        self_
            .vm
            .sys_complete_promise(key, NonEmptyValue::Success(buffer.as_bytes().to_vec()))
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_complete_promise_failure(
        mut self_: PyRefMut<'_, Self>,
        key: String,
        value: PyFailure,
    ) -> Result<PyAsyncResultHandle, PyVMError> {
        self_
            .vm
            .sys_complete_promise(key, NonEmptyValue::Failure(value.into()))
            .map(Into::into)
            .map_err(Into::into)
    }

    /// Returns either:
    ///
    /// * `PyBytes`, in case the run was executed with success
    /// * `PyFailure`, in case the run was executed with failure
    /// * `None` in case the run was not executed
    fn sys_run_enter(
        mut self_: PyRefMut<'_, Self>,
        name: String,
    ) -> Result<Bound<'_, PyAny>, PyVMError> {
        let result = self_.vm.sys_run_enter(name)?;

        let py = self_.py();

        Ok(match result {
            RunEnterResult::Executed(NonEmptyValue::Success(b)) => {
                PyBytes::new_bound(py, &b).into_any()
            }
            RunEnterResult::Executed(NonEmptyValue::Failure(f)) => {
                PyFailure::from(f).into_py(py).into_bound(py).into_any()
            }
            RunEnterResult::NotExecuted => PyNone::get_bound(py).to_owned().into_any(),
        })
    }

    fn sys_run_exit_success(
        mut self_: PyRefMut<'_, Self>,
        buffer: &Bound<'_, PyBytes>,
    ) -> Result<PyAsyncResultHandle, PyVMError> {
        self_
            .vm
            .sys_run_exit(NonEmptyValue::Success(buffer.as_bytes().to_vec()))
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_run_exit_failure(
        mut self_: PyRefMut<'_, Self>,
        value: PyFailure,
    ) -> Result<PyAsyncResultHandle, PyVMError> {
        self_
            .vm
            .sys_run_exit(NonEmptyValue::Failure(value.into()))
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_write_output_success(
        mut self_: PyRefMut<'_, Self>,
        buffer: &Bound<'_, PyBytes>,
    ) -> Result<(), PyVMError> {
        self_
            .vm
            .sys_write_output(NonEmptyValue::Success(buffer.as_bytes().to_vec()))
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_write_output_failure(
        mut self_: PyRefMut<'_, Self>,
        value: PyFailure,
    ) -> Result<(), PyVMError> {
        self_
            .vm
            .sys_write_output(NonEmptyValue::Failure(value.into()))
            .map(Into::into)
            .map_err(Into::into)
    }

    fn sys_end(mut self_: PyRefMut<'_, Self>) -> Result<(), PyVMError> {
        self_.vm.sys_end().map(Into::into).map_err(Into::into)
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
    m.add_class::<PySuspended>()?;
    m.add_class::<PyVM>()?;
    m.add("VMException", m.py().get_type_bound::<VMException>())?;
    Ok(())
}
