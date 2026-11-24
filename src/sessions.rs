//! Sessions: an object's SQLite-compatible database, kept in its Restate
//! state.
//!
//! The storage and the SQL on it are shared-core's (`sql::Database`, with its
//! feature `sql`); this module binds them to Python, as `native/src/node.rs`
//! of sdk-sessions binds them to JavaScript. `restate/sessions/_database.py`
//! opens a database, and orders the calls.
//!
//! # Where a call runs
//!
//! On the thread of the asyncio loop that makes it, and nowhere else
//! ([`SqlTask`]): shared-core's database creates no thread, and runs on the
//! task that polls it. A call that needs nothing it must wait for, such as SQL
//! on cached pages or a statement inside a transaction, returns its value at
//! once, or raises its error ([`answer`]); one that waits returns an asyncio
//! future, and is polled again on the loop as what it waits for completes.
//! Two other kinds of thread wake a call: the page cache's disk tier
//! (foyer's runtime), and, with an object store, the tokio runtime that
//! drives its requests' connections and timers ([`InRuntime`]). Their
//! wake-ups reach the loop through a thread of their own ([`foreign_wakes`]),
//! so that neither ever waits for the GIL.
//!
//! # The store
//!
//! The state the database is kept in: `set` and `clear` queue a write; `sync`
//! completes once every write before it is stored. The object's state keeps
//! writes in issue order, so a crash loses only a suffix of them (see
//! [`KvStore`]): a statement that commits queues its writes, then completes
//! once the write of its head is acked. With `keys`, a checkpoint deletes the
//! packs no checkpoint needs; a store without keeps its garbage.
//!
//! The store is the attempt's VM (`PyExplicitRecoveryVM`, see
//! [`crate::explicit_recovery`]), which never replays a journal ([`VmStore`]):
//! reads, writes, the listing of the keys and the acks are its actions, and
//! the store awaits their completions itself, in Rust. Only what a commit
//! makes besides (a transaction's outbox sends) goes through Python
//! (`COMMIT`). Or, without a VM (in tests, or with a journal codec), a store
//! implemented in Python ([`PyStore`]). Either way, Python's side is one
//! function, `dispatch(op, arg, value, reply)`, called directly, on the
//! loop's thread, in the order of the requests, so that a read sees the
//! writes before it.
//!
//! Every database of the process is kept warm in one page cache
//! ([`page_cache`]), and keeps the checkpoints it outgrows Restate with in
//! the object store `RESTATE_SQL_OBJECT_STORE` names, if any
//! ([`object_store`]), past the share `RESTATE_SQL_L0_BYTES` leaves Restate
//! ([`state_options`]). That store's requests are reqwest's, which need a
//! tokio runtime: they alone are polled inside one ([`InRuntime`]).
use crate::explicit_recovery::{vm_error, PyExplicitRecoveryVM};
use bytes::Bytes;
use futures::channel::oneshot;
use futures::future::BoxFuture;
use pyo3::create_exception;
use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyByteArray, PyBytes, PyFloat, PyInt, PyList, PyString, PyTuple};
use pyo3::IntoPyObjectExt;
use restate_sdk_shared_core::explicit_recovery::ExplicitRecoveryVM;
use restate_sdk_shared_core::sql::{self, Numeric, Value};
use restate_sdk_shared_core::state::{
    self, BlobStore, KvStore, ObjectStoreBlobs, ObjectStoreL1, PageCache, PageCacheConfig,
    StateOptions,
};
use restate_sdk_shared_core::NotificationHandle;
use restate_sdk_shared_core::Value as VmValue;
use std::borrow::Cow;
use std::collections::HashMap;
use std::future::Future;
use std::io;
use std::ops::Range;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex, OnceLock};
use std::task::{Context, Poll, Wake, Waker};
use std::thread::ThreadId;

create_exception!(
    restate_sdk_python_core,
    SqlRefusedException,
    pyo3::exceptions::PyException,
    "A failure of the database that a retry would repeat (shared-core's `sql::Refused`): it comes of what the host gave, its SQL, its records, a transaction it left open. It may have ended the session."
);

/// What a request to Python's side does: the store's method it calls.
pub(crate) const GET: u32 = 0;
pub(crate) const SET: u32 = 1;
pub(crate) const CLEAR: u32 = 2;
pub(crate) const SYNC: u32 = 3;
pub(crate) const KEYS: u32 = 4;
/// Run what the committing transaction makes at its commit (its outbox's sends): between the
/// commit's acked write of the head and the wait for its ack ([`VmStore`]).
pub(crate) const COMMIT: u32 = 5;

// --- Running calls on the loop's thread ------------------------------------------------------------

/// A call to the database, its value a Python object.
type Call = Pin<Box<dyn Future<Output = PyResult<Py<PyAny>>> + Send>>;

/// A call that waits: polled on its loop's thread, as what it waits for wakes
/// it, until it settles its asyncio future. A cancelled future drops the call,
/// which ends its session (shared-core: a call that does not return leaves its
/// state unknown).
#[pyclass(frozen)]
struct SqlTask {
    call: Mutex<Option<Call>>,
    future: Py<PyAny>,
    event_loop: Py<PyAny>,
    /// The loop's thread.
    thread: ThreadId,
    /// Whether a poll is scheduled on the loop already.
    scheduled: AtomicBool,
}

#[pymethods]
impl SqlTask {
    /// Poll the call once more: what the waker schedules on the loop.
    fn _poll(slf: Py<Self>, py: Python<'_>) -> PyResult<()> {
        poll(py, &slf)
    }

    /// The future's done-callback: a cancelled one drops its call.
    fn _done(&self, future: &Bound<'_, PyAny>) -> PyResult<()> {
        if future.call_method0("cancelled")?.is_truthy()? {
            if let Ok(mut call) = self.call.try_lock() {
                call.take();
            }
        }
        Ok(())
    }
}

/// `asyncio.get_running_loop`.
static GET_RUNNING_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

impl SqlTask {
    /// Run `call`, which a first poll left pending, on the running loop: its
    /// asyncio future.
    fn spawn<'py>(py: Python<'py>, call: Call) -> PyResult<Bound<'py, PyAny>> {
        let event_loop = GET_RUNNING_LOOP
            .import(py, "asyncio", "get_running_loop")?
            .call0()?;
        let future = event_loop.call_method0("create_future")?;
        let task = Py::new(
            py,
            SqlTask {
                call: Mutex::new(Some(call)),
                future: future.clone().unbind(),
                event_loop: event_loop.unbind(),
                thread: std::thread::current().id(),
                scheduled: AtomicBool::new(false),
            },
        )?;
        future.call_method1("add_done_callback", (task.bind(py).getattr("_done")?,))?;
        poll(py, &task)?;
        Ok(future)
    }
}

/// Poll `task`'s call, settling its future once it is ready.
fn poll(py: Python<'_>, task: &Py<SqlTask>) -> PyResult<()> {
    let state = task.get();
    state.scheduled.store(false, Ordering::Release);
    let mut slot = state
        .call
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let Some(call) = slot.as_mut() else {
        return Ok(());
    };
    let future = state.future.bind(py);
    if future.call_method0("done")?.is_truthy()? {
        slot.take();
        return Ok(());
    }
    let waker = Waker::from(Arc::new(TaskWaker(task.clone_ref(py))));
    let Poll::Ready(result) = call.as_mut().poll(&mut Context::from_waker(&waker)) else {
        return Ok(());
    };
    slot.take();
    drop(slot);
    match result {
        Ok(value) => future.call_method1("set_result", (value,))?,
        Err(error) => future.call_method1("set_exception", (error.into_value(py),))?,
    };
    Ok(())
}

/// Wakes a [`SqlTask`]: a poll on its loop, scheduled once however often it
/// is woken before it runs. Woken on the loop's thread, it schedules it
/// there; woken on another (the page cache's disk tier, or the object
/// store's tokio runtime), it hands the scheduling to [`foreign_wakes`], so
/// that the waking thread never waits for the GIL.
struct TaskWaker(Py<SqlTask>);

impl Wake for TaskWaker {
    fn wake(self: Arc<Self>) {
        self.wake_by_ref();
    }

    fn wake_by_ref(self: &Arc<Self>) {
        let task = self.0.get();
        if task.scheduled.swap(true, Ordering::AcqRel) {
            return;
        }
        if std::thread::current().id() == task.thread {
            Python::attach(|py| schedule(py, &self.0, false));
        } else if foreign_wakes().send(self.clone()).is_err() {
            task.scheduled.store(false, Ordering::Release);
        }
    }
}

/// The thread that schedules the wake-ups of other threads on their loops.
fn foreign_wakes() -> &'static mpsc::Sender<Arc<TaskWaker>> {
    static WAKES: OnceLock<mpsc::Sender<Arc<TaskWaker>>> = OnceLock::new();
    WAKES.get_or_init(|| {
        let (wakes, woken) = mpsc::channel::<Arc<TaskWaker>>();
        std::thread::Builder::new()
            .name("restate-sql-wake".into())
            .spawn(move || {
                for waker in woken {
                    // None once the interpreter is gone: nothing more runs.
                    let _ = Python::try_attach(|py| schedule(py, &waker.0, true));
                }
            })
            .expect("the thread of the page cache's wake-ups");
        wakes
    })
}

/// Schedule a poll of `task` on its loop; a closed loop runs nothing more.
fn schedule(py: Python<'_>, task: &Py<SqlTask>, threadsafe: bool) {
    let state = task.get();
    let scheduled = task.bind(py).getattr("_poll").and_then(|poll| {
        let method = if threadsafe {
            "call_soon_threadsafe"
        } else {
            "call_soon"
        };
        state.event_loop.call_method1(py, method, (poll,))
    });
    if scheduled.is_err() {
        state.scheduled.store(false, Ordering::Release);
    }
}

/// `call`'s value if a first poll finds it ready, on the calling thread; else
/// an asyncio future of it ([`SqlTask`]). Ready, a call costs no future: SQL
/// on cached pages, or a statement inside a transaction.
fn answer<'py, T, F>(py: Python<'py>, call: F) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = PyResult<T>> + Send + 'static,
    T: for<'a> IntoPyObject<'a> + Send + 'static,
{
    let mut call: Call = Box::pin(async move {
        let value = call.await?;
        Python::attach(|py| value.into_py_any(py))
    });
    match call.as_mut().poll(&mut Context::from_waker(Waker::noop())) {
        Poll::Ready(value) => value.map(|value| value.into_bound(py)),
        Poll::Pending => SqlTask::spawn(py, call),
    }
}

// --- Python's side of the store --------------------------------------------------------------------

/// What a request answers: a `get`'s value, the store's keys, or nothing.
enum Answer {
    Nothing,
    Value(Bytes),
    Keys(Vec<String>),
}

/// The callable that `dispatch` calls once with a request's outcome:
/// `reply(value)` or `reply(None, error)`. The value is a `get`'s (`bytes`,
/// or `None` when absent), the store's keys (a `list[str]`), or `None`.
#[pyclass(frozen)]
struct SqlReply {
    sender: Mutex<Option<oneshot::Sender<Result<Answer, String>>>>,
}

#[pymethods]
impl SqlReply {
    /// A value that is none of those fails the request, with why.
    #[pyo3(signature = (value, error=None))]
    fn __call__(&self, value: Option<Bound<'_, PyAny>>, error: Option<String>) {
        let answer = match (error, value) {
            (Some(error), _) => Err(error),
            (None, None) => Ok(Answer::Nothing),
            (None, Some(value)) => answer_of(&value).map_err(|e| e.to_string()),
        };
        let sender = self.sender.lock().ok().and_then(|mut sender| sender.take());
        if let Some(sender) = sender {
            let _ = sender.send(answer);
        }
    }
}

/// A request's answer from the value the store gave: its keys (a list), or a value read.
fn answer_of(value: &Bound<'_, PyAny>) -> PyResult<Answer> {
    if value.is_instance_of::<PyList>() {
        Ok(Answer::Keys(value.extract()?))
    } else {
        Ok(Answer::Value(Bytes::from(bytes_of(value)?)))
    }
}

/// The bytes of `bytes`, `bytearray` or `memoryview`.
fn bytes_of(value: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    if let Ok(bytes) = value.cast::<PyBytes>() {
        Ok(bytes.as_bytes().to_vec())
    } else if value.is_instance_of::<PyByteArray>() {
        value.extract()
    } else if let Ok(bytes) = value.call_method0("tobytes") {
        Ok(bytes.cast::<PyBytes>()?.as_bytes().to_vec())
    } else {
        Err(PyTypeError::new_err(format!(
            "expected bytes, got {}",
            value.get_type().name()?
        )))
    }
}

fn io_error(error: impl std::fmt::Display) -> io::Error {
    io::Error::other(error.to_string())
}

/// Python's side of a store: `dispatch(op, arg, value, reply)`, called on
/// the loop's thread, where every call runs.
struct Dispatch(Py<PyAny>);

impl Dispatch {
    /// `dispatch(op, arg, value, reply)`; `reply` is `None` for a write, which answers nothing.
    fn call(
        &self,
        op: u32,
        key: &str,
        value: Option<Bytes>,
        reply: Option<SqlReply>,
    ) -> io::Result<()> {
        Python::attach(|py| {
            let key = PyString::new(py, key);
            let value = value.map(|value| PyBytes::new(py, &value).into_any().unbind());
            let reply = match reply {
                Some(reply) => Py::new(py, reply)?.into_any(),
                None => py.None(),
            };
            self.0.call1(py, (op, key, value, reply))?;
            Ok::<_, PyErr>(())
        })
        .map_err(io_error)
    }

    /// A request that answers: a read, a sync, the keys, or a commit's sends.
    async fn request(&self, op: u32, key: &str) -> io::Result<Answer> {
        let (sender, receiver) = oneshot::channel();
        let reply = SqlReply {
            sender: Mutex::new(Some(sender)),
        };
        self.call(op, key, None, Some(reply))?;
        match receiver.await {
            Ok(Ok(answer)) => Ok(answer),
            Ok(Err(error)) => Err(io_error(error)),
            Err(_) => Err(io_error("the store dropped the request")),
        }
    }

    async fn sync(&self) -> io::Result<()> {
        self.request(SYNC, "").await.map(drop)
    }

    async fn keys(&self) -> io::Result<Vec<String>> {
        match self.request(KEYS, "").await? {
            Answer::Keys(keys) => Ok(keys),
            _ => Err(unexpected("keys")),
        }
    }
}

fn unexpected(method: &str) -> io::Error {
    io_error(format!("the store's {method} answered with the wrong type"))
}

fn no_keys() -> io::Error {
    io::Error::new(io::ErrorKind::Unsupported, "the store has no keys")
}

/// A store implemented in Python: every request goes to `dispatch`.
struct PyStore {
    dispatch: Dispatch,
    /// Whether the store has `keys`.
    lists_keys: bool,
}

impl KvStore for PyStore {
    fn get<'a>(&'a self, key: &'a str) -> BoxFuture<'a, io::Result<Option<Bytes>>> {
        Box::pin(async move {
            match self.dispatch.request(GET, key).await? {
                Answer::Nothing => Ok(None),
                Answer::Value(value) => Ok(Some(value)),
                Answer::Keys(_) => Err(unexpected("get")),
            }
        })
    }

    fn put(&self, key: &str, value: Bytes) -> io::Result<()> {
        self.dispatch.call(SET, key, Some(value), None)
    }

    fn delete(&self, key: &str) -> io::Result<()> {
        self.dispatch.call(CLEAR, key, None, None)
    }

    fn sync(&self) -> BoxFuture<'_, io::Result<()>> {
        Box::pin(self.dispatch.sync())
    }

    fn keys(&self) -> BoxFuture<'_, io::Result<Vec<String>>> {
        Box::pin(async move {
            match self.lists_keys {
                true => self.dispatch.keys().await,
                false => Err(no_keys()),
            }
        })
    }
}

// --- The invocation's VM as the store --------------------------------------------------------------

/// The state key of the store's writes that only ask for an ack ([`VmStore::sync`]): under the
/// state format's prefix, which tooling hides.
const SYNC_KEY: &str = "v22/sync";

/// The object's state through the attempt's VM (`PyExplicitRecoveryVM`), without Python on the
/// way: a read, a write and the listing of the keys are the VM's actions, and the store awaits
/// their completions itself ([`PyExplicitRecoveryVM::wait_in_rust`]).
///
/// The sync is Restate's ack of a write: a flush writes its head with
/// [`KvStore::put_and_sync`], an acked write of the head, and since Restate stores writes in
/// order, the ack means every earlier write is stored too. Between the head and the wait for
/// its ack, Python runs what the committing transaction makes at its commit (`COMMIT`, its
/// outbox's sends). A sync with writes since the last ack (garbage collection, an open)
/// clears [`SYNC_KEY`] with an ack.
///
/// Each action is named after its key, so that tooling can hide the database by name. With
/// `name`, the database `name` of the key, apart from the object's own: each key under
/// `db/<name>/` (see `_beside` in `restate/sessions/_session.py`). A reader (a view's) writes
/// nothing. After each call, the VM's scheduler has Python write the output.
struct VmStore {
    vm: Py<PyExplicitRecoveryVM>,
    dispatch: Dispatch,
    name: Option<String>,
    reader: bool,
    /// The writes issued, and how many of them an ack covers.
    issued: AtomicU64,
    synced: AtomicU64,
}

impl VmStore {
    /// The state key of the database's `key`.
    fn key(&self, key: &str) -> String {
        match &self.name {
            None => key.to_owned(),
            Some(name) => {
                let at = key.find('/').map_or(0, |at| at + 1);
                format!("{}db/{name}/{}", &key[..at], &key[at..])
            }
        }
    }

    /// `f` of the VM, borrowed for that long (never across an await); then the scheduler, so
    /// that Python writes what `f` sent.
    fn act<T>(&self, f: impl FnOnce(&mut PyExplicitRecoveryVM) -> PyResult<T>) -> io::Result<T> {
        Python::attach(|py| {
            let (result, scheduler) = {
                let mut vm = self.vm.bind(py).try_borrow_mut().map_err(io_error)?;
                let result = f(&mut vm);
                (result, vm.scheduler(py))
            };
            if let Some(scheduler) = scheduler {
                scheduler.call0(py).map_err(io_error)?;
            }
            result.map_err(io_error)
        })
    }

    /// The notification of `handle`.
    async fn wait(&self, handle: u32) -> io::Result<VmValue> {
        let notification = self.act(|vm| Ok(vm.wait_in_rust(NotificationHandle::from(handle))))?;
        match notification.await {
            Ok(Ok(value)) => Ok(value),
            Ok(Err(error)) => Err(io_error(vm_error(error))),
            Err(_) => Err(io_error("the attempt ended")),
        }
    }

    /// Issue a write with `f`: its count, and its handle (notified by the ack, if it asks for one).
    fn write(
        &self,
        f: impl FnOnce(
            &mut PyExplicitRecoveryVM,
        ) -> Result<NotificationHandle, restate_sdk_shared_core::Error>,
    ) -> io::Result<(u64, u32)> {
        if self.reader {
            return Err(io_error("a view writes no state"));
        }
        let handle = self.act(|vm| f(vm).map(u32::from).map_err(vm_error))?;
        Ok((self.issued.fetch_add(1, Ordering::SeqCst) + 1, handle))
    }

    /// Wait for the ack of the write `issued`, after running the committing transaction's
    /// callbacks: every write issued up to it is then stored.
    async fn acked(&self, issued: u64, handle: u32) -> io::Result<()> {
        self.dispatch.request(COMMIT, "").await?;
        self.wait(handle).await?;
        self.synced.fetch_max(issued, Ordering::SeqCst);
        Ok(())
    }
}

/// The database `name`'s key of the state key `key`, if it is one of it: `_unbeside` of
/// `restate/sessions/_session.py`.
fn unbeside(name: &str, key: &str) -> Option<String> {
    let at = key.find('/')? + 1;
    let rest = key[at..].strip_prefix(&format!("db/{name}/"))?;
    Some(format!("{}{rest}", &key[..at]))
}

impl KvStore for VmStore {
    fn get<'a>(&'a self, key: &'a str) -> BoxFuture<'a, io::Result<Option<Bytes>>> {
        Box::pin(async move {
            let key = self.key(key);
            let handle = self.act(|vm| {
                vm.vm
                    .cmd_state_get(key.clone(), Some(key))
                    .map(u32::from)
                    .map_err(vm_error)
            })?;
            match self.wait(handle).await? {
                VmValue::Success(value) => Ok(Some(value)),
                VmValue::Void => Ok(None),
                VmValue::Failure(failure) => Err(io_error(failure.message)),
                _ => Err(io_error("the state read completed with no value")),
            }
        })
    }

    fn put(&self, key: &str, value: Bytes) -> io::Result<()> {
        let key = self.key(key);
        self.write(|vm| vm.vm.cmd_state_set(key.clone(), value, Some(key), false))
            .map(drop)
    }

    fn delete(&self, key: &str) -> io::Result<()> {
        let key = self.key(key);
        self.write(|vm| vm.vm.cmd_state_clear(key.clone(), Some(key), false))
            .map(drop)
    }

    fn put_and_sync<'a>(&'a self, key: &'a str, value: Bytes) -> BoxFuture<'a, io::Result<()>> {
        let key = self.key(key);
        // Issued now, as a put is.
        let written = self.write(|vm| vm.vm.cmd_state_set(key.clone(), value, Some(key), true));
        Box::pin(async move {
            let (issued, handle) = written?;
            self.acked(issued, handle).await
        })
    }

    fn sync(&self) -> BoxFuture<'_, io::Result<()>> {
        Box::pin(async move {
            if self.reader {
                // A reader wrote nothing to sync.
                return Ok(());
            }
            if self.synced.load(Ordering::SeqCst) == self.issued.load(Ordering::SeqCst) {
                self.dispatch.request(COMMIT, "").await?;
                return Ok(());
            }
            let key = self.key(SYNC_KEY);
            let (issued, handle) =
                self.write(|vm| vm.vm.cmd_state_clear(key.clone(), Some(key), true))?;
            self.acked(issued, handle).await
        })
    }

    fn keys(&self) -> BoxFuture<'_, io::Result<Vec<String>>> {
        Box::pin(async move {
            if self.reader {
                return Err(no_keys());
            }
            // Named under the state format's prefix, which tooling hides.
            let handle = self.act(|vm| {
                vm.vm
                    .cmd_state_get_keys(Some("v22/keys".to_owned()))
                    .map(u32::from)
                    .map_err(vm_error)
            })?;
            let keys = match self.wait(handle).await? {
                VmValue::StateKeys(keys) => keys,
                _ => return Err(unexpected("keys")),
            };
            Ok(match &self.name {
                None => keys,
                Some(name) => keys.iter().filter_map(|key| unbeside(name, key)).collect(),
            })
        })
    }
}

// --- The database ----------------------------------------------------------------------------------

/// A failure of the database as Python gets it: one a retry would repeat
/// ([`sql::Refused`]) is an `SqlRefusedException`, which
/// `restate/sessions/_database.py` tells apart, as it may have ended the
/// session; any other a `RuntimeError`.
fn error(error: Box<dyn std::error::Error + Send + Sync>) -> PyErr {
    match error.is::<sql::Refused>() {
        true => SqlRefusedException::new_err(error.to_string()),
        false => PyRuntimeError::new_err(error.to_string()),
    }
}

/// The page cache shared by the process's databases: 256 MiB of memory and
/// 1 GiB of disk, in a file under the temporary directory, or the file
/// `RESTATE_SQL_CACHE_FILE` names. `RESTATE_SQL_CACHE_MEMORY` and
/// `RESTATE_SQL_CACHE_DISK` set its sizes (bytes, or with KiB, MiB or GiB);
/// the memory one also bounds the namespaces' states and the parked engines
/// it keeps, so a small cache is a cold one. `RESTATE_SQL_CACHE=off` (or `0`,
/// `false`, `no`) opens every database without a cache (`None`): each session
/// reads what it needs from the store. A size that is not one fails every
/// open, with its message: the process serves, and every invocation retries.
async fn page_cache() -> io::Result<Option<PageCache>> {
    static CACHE: futures::lock::Mutex<Option<Option<PageCache>>> = futures::lock::Mutex::new(None);
    let mut cache = CACHE.lock().await;
    if let Some(cache) = &*cache {
        return Ok(cache.clone());
    }
    let opened = match cache_config()? {
        Some(config) => Some(PageCache::open(config).await?),
        None => None,
    };
    Ok(cache.insert(opened).clone())
}

/// The page cache's configuration, from the environment: `None` when it is off.
fn cache_config() -> io::Result<Option<PageCacheConfig>> {
    let off = var("RESTATE_SQL_CACHE").map(|v| v.to_ascii_lowercase());
    if matches!(off.as_deref(), Some("off" | "0" | "false" | "no")) {
        return Ok(None);
    }
    Ok(Some(PageCacheConfig {
        file: std::env::var_os("RESTATE_SQL_CACHE_FILE").map(std::path::PathBuf::from),
        ..PageCacheConfig::sized(
            size("RESTATE_SQL_CACHE_MEMORY")?.unwrap_or(256 << 20),
            size("RESTATE_SQL_CACHE_DISK")?.unwrap_or(1 << 30),
        )
    }))
}

/// The environment variable `name`, unless unset or empty.
fn var(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.is_empty())
}

/// The size the environment variable `name` gives, if set: bytes, or with
/// KiB, MiB or GiB ([`state::parse_size`]). One that is not a size is an
/// error that says so.
fn size(name: &str) -> io::Result<Option<usize>> {
    let Some(value) = var(name) else {
        return Ok(None);
    };
    match state::parse_size(&value) {
        Some(size) => Ok(Some(size)),
        None => Err(io::Error::other(format!(
            "{name}={value}: not a size (bytes, or with KiB, MiB or GiB)"
        ))),
    }
}

/// How the process's databases flush: shared-core's defaults
/// ([`StateOptions::default`]), with L0's share from `RESTATE_SQL_L0_BYTES`,
/// read once: a database keeps its checkpoints' packs in the object's state
/// (L0) until they hold this many bytes, and the checkpoint after that moves
/// them, with its own, to the object store ([`StateOptions::l0_bytes`], 4
/// MiB by default; bytes, or with KiB, MiB or GiB). `0` puts every
/// checkpoint's pack in the object store. Without an object store it changes
/// nothing: every pack stays in the state. A size that is not one fails
/// every open, with its message, as a page cache size that is not one does.
///
/// It may differ from one process, or open, to the next: it never changes
/// what is stored, only where the next checkpoint puts its pack. Every
/// pack's name says its tier, which is where reads and garbage collection
/// look for it, so a lower one moves what L0 holds at the next checkpoint,
/// and a higher one keeps more packs in the state from then on; packs never
/// move back from the object store.
fn state_options() -> io::Result<StateOptions> {
    static L0_BYTES: Mutex<Option<u64>> = Mutex::new(None);
    let mut l0 = L0_BYTES
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let defaults = StateOptions::default();
    let l0_bytes = match *l0 {
        Some(bytes) => bytes,
        None => {
            let set = size("RESTATE_SQL_L0_BYTES")?;
            *l0.insert(set.map_or(defaults.l0_bytes, |bytes| bytes as u64))
        }
    };
    Ok(StateOptions {
        l0_bytes,
        ..defaults
    })
}

/// The process's object store, the one `RESTATE_SQL_OBJECT_STORE` names
/// ([`ObjectStoreL1::from_env`]: `s3://bucket/path`, `gs://bucket/path`,
/// `az://container/path` or `file:///a/directory`, with the provider's own
/// `AWS_*`, `GOOGLE_*` or `AZURE_*` variables), made once: `None` when it is
/// unset or empty, and every pack stays in Restate. Its databases share it,
/// and its HTTP client. A value that names no store fails every open, with
/// its message, as a page cache size that is not one does. Once a database
/// has moved packs to the store, every process that serves it needs the same
/// value, or its opens fail.
fn object_store() -> io::Result<Option<ObjectStoreL1>> {
    static L1: Mutex<Option<Option<ObjectStoreL1>>> = Mutex::new(None);
    let mut l1 = L1.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    if let Some(l1) = &*l1 {
        return Ok(l1.clone());
    }
    Ok(l1.insert(ObjectStoreL1::from_env()?).clone())
}

/// A database's packs in the object store, each request polled inside
/// pyo3-async-runtimes' tokio runtime (the tunnel's too). The store's HTTP
/// client, reqwest, takes its connections and timers from the current
/// runtime, and a call is polled on the asyncio loop's thread, outside any
/// ([`SqlTask`]); the runtime's threads drive them, and wake the call as
/// any other thread does ([`TaskWaker`]). Nothing else is polled inside it.
struct InRuntime(ObjectStoreBlobs);

impl BlobStore for InRuntime {
    fn get_range<'a>(
        &'a self,
        key: &'a str,
        range: Range<u64>,
    ) -> BoxFuture<'a, io::Result<Option<Bytes>>> {
        in_runtime(self.0.get_range(key, range))
    }

    fn put<'a>(&'a self, key: &'a str, value: Bytes) -> BoxFuture<'a, io::Result<()>> {
        in_runtime(self.0.put(key, value))
    }

    fn delete<'a>(&'a self, keys: &'a [String]) -> BoxFuture<'a, io::Result<()>> {
        in_runtime(self.0.delete(keys))
    }

    fn list<'a>(&'a self, prefix: &'a str) -> BoxFuture<'a, io::Result<Vec<String>>> {
        in_runtime(self.0.list(prefix))
    }
}

/// `future`, each poll inside the runtime's context.
fn in_runtime<'a, T: 'a>(mut future: BoxFuture<'a, T>) -> BoxFuture<'a, T> {
    let runtime = pyo3_async_runtimes::tokio::get_runtime();
    Box::pin(std::future::poll_fn(move |cx| {
        let _entered = runtime.enter();
        future.as_mut().poll(cx)
    }))
}

/// Open the database of `namespace`, kept warm in the process's page cache,
/// with its packs past L0 ([`state_options`]) in the process's object store, if any ([`object_store`]),
/// over the invocation's `vm` if given (with `name`, the key's database
/// `name`), or else over the store behind `dispatch` (`lists_keys` if it has
/// `keys`): see the module's doc. See [`sql::Open::reader`] for a `reader`.
#[pyfunction]
#[pyo3(signature = (namespace, dispatch, lists_keys = false, reader = false, vm = None, name = None))]
pub(crate) fn sql_open<'py>(
    py: Python<'py>,
    namespace: String,
    dispatch: Py<PyAny>,
    lists_keys: bool,
    reader: bool,
    vm: Option<Py<PyExplicitRecoveryVM>>,
    name: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let dispatch = Dispatch(dispatch);
    let store: Arc<dyn KvStore> = match vm {
        Some(vm) => Arc::new(VmStore {
            vm,
            dispatch,
            name,
            reader,
            issued: AtomicU64::new(0),
            synced: AtomicU64::new(0),
        }),
        None => Arc::new(PyStore {
            dispatch,
            lists_keys,
        }),
    };
    answer(py, async move {
        let options = state_options().map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        let mut open = sql::Database::open(store).options(options);
        let cache = page_cache()
            .await
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        if let Some(cache) = &cache {
            open = open.cached(cache, &namespace);
        }
        let l1 = object_store().map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        if let Some(l1) = l1 {
            open = open.checkpoints(Arc::new(InRuntime(l1.blobs(&namespace))));
        }
        if reader {
            open = open.reader();
        }
        Ok(PySqlDatabase(Arc::new(open.await.map_err(error)?)))
    })
}

/// One session of a namespace's database (see [`sql::Database`]): every SQL
/// commit is durable when its call completes. Each call returns its value, or
/// an asyncio future of it (see [`answer`]).
#[pyclass(frozen)]
pub(crate) struct PySqlDatabase(Arc<sql::Database>);

#[pymethods]
impl PySqlDatabase {
    /// Run SQL (one or more statements; parameters bind to a single one) and
    /// return `(columns, rows)`: the rows of its last statement, with its
    /// column names. A commit is durable when this completes.
    #[pyo3(signature = (sql, params=None))]
    fn query_columns<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let params = params.map(|params| sql_params(&params)).transpose()?;
        let db = self.0.clone();
        answer(py, async move {
            let (columns, rows) = db.query_columns(&sql, params).await.map_err(error)?;
            Ok(QueryResult { columns, rows })
        })
    }

    /// See [`sql::Database::begin`].
    fn begin<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let db = self.0.clone();
        answer(py, async move { db.begin().await.map_err(error) })
    }

    /// See [`sql::Database::commit`]: `records` by name.
    #[pyo3(signature = (records=None))]
    fn commit<'py>(
        &self,
        py: Python<'py>,
        records: Option<HashMap<String, Vec<u8>>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let records: Vec<_> = (records.unwrap_or_default().into_iter())
            .map(|(name, value)| (name, Bytes::from(value)))
            .collect();
        let db = self.0.clone();
        answer(py, async move { db.commit(records).await.map_err(error) })
    }

    /// See [`sql::Database::rollback`].
    fn rollback<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let db = self.0.clone();
        answer(py, async move { db.rollback().await.map_err(error) })
    }

    /// See [`sql::Database::is_usable`].
    fn is_usable(&self) -> bool {
        self.0.is_usable()
    }

    /// See [`sql::Database::record`]: `bytes`.
    fn record<'py>(&self, py: Python<'py>, name: String) -> PyResult<Bound<'py, PyAny>> {
        let db = self.0.clone();
        answer(py, async move {
            let value = db.record(&name).await.map_err(error)?;
            Ok(Cow::<'static, [u8]>::Owned(value.to_vec()))
        })
    }

    /// See [`sql::Database::save`].
    fn save<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let db = self.0.clone();
        answer(py, async move { db.save().await.map_err(error) })
    }

    /// See [`sql::Database::close`].
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let db = self.0.clone();
        answer(py, async move {
            db.close().await;
            Ok(())
        })
    }
}

/// Rows with the column names of their statement: `(columns, rows)` in Python.
struct QueryResult {
    columns: Vec<String>,
    rows: sql::Rows,
}

impl<'py> IntoPyObject<'py> for QueryResult {
    type Target = PyTuple;
    type Output = Bound<'py, PyTuple>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        let rows = PyList::empty(py);
        for row in self.rows {
            let values = row
                .into_iter()
                .map(|value| python_value(py, value))
                .collect::<PyResult<Vec<_>>>()?;
            rows.append(PyList::new(py, values)?)?;
        }
        PyTuple::new(
            py,
            [PyList::new(py, self.columns)?.into_any(), rows.into_any()],
        )
    }
}

/// SQL values from Python: `None`, `bool` (0 or 1), `int` (64 bits),
/// `float`, `str`, and `bytes`, `bytearray` or `memoryview` (BLOB).
fn sql_params(params: &Bound<'_, PyAny>) -> PyResult<Vec<Value>> {
    if params.is_instance_of::<PyString>() || params.is_instance_of::<PyBytes>() {
        return Err(PyTypeError::new_err(
            "SQL parameters are a sequence of values",
        ));
    }
    params.try_iter()?.map(|param| sql_value(&param?)).collect()
}

fn sql_value(value: &Bound<'_, PyAny>) -> PyResult<Value> {
    if value.is_none() {
        Ok(Value::Null)
    } else if value.is_instance_of::<PyBool>() {
        Ok(Value::from_i64(i64::from(value.extract::<bool>()?)))
    } else if value.is_instance_of::<PyInt>() {
        let number = value
            .extract::<i64>()
            .map_err(|_| PyValueError::new_err("an integer parameter exceeds 64 bits"))?;
        Ok(Value::from_i64(number))
    } else if value.is_instance_of::<PyFloat>() {
        Ok(Value::from_f64(value.extract::<f64>()?))
    } else if value.is_instance_of::<PyString>() {
        Ok(Value::build_text(value.extract::<String>()?))
    } else {
        let bytes = bytes_of(value).map_err(|_| {
            let name = value.get_type().name().map(|n| n.to_string());
            PyTypeError::new_err(format!(
                "unsupported SQL value of type {}",
                name.unwrap_or_default()
            ))
        })?;
        Value::from_slice(&bytes).map_err(|e| PyValueError::new_err(e.to_string()))
    }
}

/// A value of a row: `None`, `int`, `float`, `str` or `bytes`.
fn python_value(py: Python<'_>, value: Value) -> PyResult<Bound<'_, PyAny>> {
    match value {
        Value::Null => Ok(py.None().into_bound(py)),
        Value::Numeric(Numeric::Integer(i)) => i.into_bound_py_any(py),
        Value::Numeric(Numeric::Float(f)) => f64::from(f).into_bound_py_any(py),
        Value::Text(text) => Ok(PyString::new(py, text.as_str()).into_any()),
        Value::Blob(blob) => Ok(PyBytes::new(py, &blob).into_any()),
    }
}

/// Add the sessions' classes, functions and constants to the module `m`.
pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PySqlDatabase>()?;
    m.add_class::<SqlReply>()?;
    m.add_class::<SqlTask>()?;
    m.add_function(wrap_pyfunction!(sql_open, m)?)?;
    m.add(
        "SqlRefusedException",
        m.py().get_type::<SqlRefusedException>(),
    )?;
    m.add("SQL_GET", GET)?;
    m.add("SQL_SET", SET)?;
    m.add("SQL_CLEAR", CLEAR)?;
    m.add("SQL_SYNC", SYNC)?;
    m.add("SQL_KEYS", KEYS)?;
    m.add("SQL_COMMIT", COMMIT)?;
    Ok(())
}
