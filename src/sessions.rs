//! Sessions: an object's SQLite-compatible database, kept in its Restate
//! state, over a key/value store implemented in Python.
//!
//! The storage and the SQL on it are shared-core's (`sql::Database`, with its
//! feature `sql`); this module binds them to Python, as `native/src/node.rs`
//! of sdk-sessions binds them to JavaScript. `restate/sessions/_database.py`
//! opens a database over a store, and orders the calls.
//!
//! `sql_open` takes the store as one dispatcher function,
//! `dispatch(op, key, value, reply)`, and the asyncio event loop it runs on.
//! Every request is scheduled on that loop with `call_soon_threadsafe`, which
//! runs callbacks in the order they were scheduled, so a read sees the writes
//! before it.
//!
//! `set` and `clear` queue a write; `sync` completes once every write before
//! it is stored. The object's state keeps writes in issue order, so a crash
//! loses only a suffix of them (see [`KvStore`]): a statement that commits
//! queues its writes, then completes at one `sync`. With `keys`, a checkpoint
//! deletes the packs no checkpoint needs; a store without keeps its garbage.
//!
//! Every database of the process is kept warm in one page cache
//! ([`page_cache`]).
//!
//! A call runs on the calling thread as far as it can ([`answer`]): one that
//! needs nothing from the store, such as SQL on cached pages or a statement
//! inside a transaction, returns its value, or raises its error; one that
//! waits for the store goes on in the tokio runtime and returns an awaitable.
//! The caller awaits the latter.
use bytes::Bytes;
use futures::channel::oneshot;
use futures::future::BoxFuture;
use pyo3::create_exception;
use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyByteArray, PyBytes, PyFloat, PyInt, PyList, PyString, PyTuple};
use pyo3::IntoPyObjectExt;
use restate_sdk_shared_core::sql::{self, Numeric, Value};
use restate_sdk_shared_core::state::{self, KvStore, PageCache, PageCacheConfig};
use std::borrow::Cow;
use std::collections::HashMap;
use std::future::Future;
use std::io;
use std::sync::{Arc, Mutex};

create_exception!(
    restate_sdk_python_core,
    SqlRefusedException,
    pyo3::exceptions::PyException,
    "A failure of the database that a retry would repeat (shared-core's `sql::Refused`): it comes of what the host gave, its SQL, its records, a transaction it left open. It may have ended the session."
);

/// What a request to the store does: the store's method it calls.
pub(crate) const GET: u32 = 0;
pub(crate) const SET: u32 = 1;
pub(crate) const CLEAR: u32 = 2;
pub(crate) const SYNC: u32 = 3;
pub(crate) const KEYS: u32 = 4;

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
    #[pyo3(signature = (value, error=None))]
    fn __call__(&self, value: Option<Bound<'_, PyAny>>, error: Option<String>) -> PyResult<()> {
        let answer = match (error, value) {
            (Some(error), _) => Err(error),
            (None, None) => Ok(Answer::Nothing),
            (None, Some(value)) => Ok(if value.is_instance_of::<PyList>() {
                Answer::Keys(value.extract()?)
            } else {
                Answer::Value(Bytes::from(bytes_of(&value)?))
            }),
        };
        let sender = self.sender.lock().ok().and_then(|mut sender| sender.take());
        if let Some(sender) = sender {
            let _ = sender.send(answer);
        }
        Ok(())
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

/// The Python store, behind its dispatcher on its event loop.
struct PyStore {
    event_loop: Py<PyAny>,
    dispatch: Py<PyAny>,
    /// Whether the store has `keys`.
    lists_keys: bool,
}

fn io_error(error: impl std::fmt::Display) -> io::Error {
    io::Error::other(error.to_string())
}

impl PyStore {
    /// Schedule `dispatch(op, key, value, reply)` on the event loop; `reply`
    /// is `None` for a write, which answers nothing.
    fn schedule(
        &self,
        op: u32,
        key: &str,
        value: Option<Bytes>,
        reply: Option<SqlReply>,
    ) -> io::Result<()> {
        Python::attach(|py| {
            let value = value.map(|value| PyBytes::new(py, &value).into_any().unbind());
            let reply = match reply {
                Some(reply) => Py::new(py, reply)?.into_any(),
                None => py.None(),
            };
            self.event_loop.call_method1(
                py,
                "call_soon_threadsafe",
                (self.dispatch.clone_ref(py), op, key, value, reply),
            )?;
            Ok::<_, PyErr>(())
        })
        .map_err(io_error)
    }

    /// A request that answers: a read, a sync, or the keys.
    async fn request(&self, op: u32, key: &str) -> io::Result<Answer> {
        let (sender, receiver) = oneshot::channel();
        let reply = SqlReply {
            sender: Mutex::new(Some(sender)),
        };
        self.schedule(op, key, None, Some(reply))?;
        match receiver.await {
            Ok(Ok(answer)) => Ok(answer),
            Ok(Err(error)) => Err(io_error(error)),
            Err(_) => Err(io_error("the store dropped the request")),
        }
    }
}

fn unexpected(method: &str) -> io::Error {
    io_error(format!("the store's {method} answered with the wrong type"))
}

impl KvStore for PyStore {
    fn get<'a>(&'a self, key: &'a str) -> BoxFuture<'a, io::Result<Option<Bytes>>> {
        Box::pin(async move {
            match self.request(GET, key).await? {
                Answer::Nothing => Ok(None),
                Answer::Value(value) => Ok(Some(value)),
                Answer::Keys(_) => Err(unexpected("get")),
            }
        })
    }

    fn put(&self, key: &str, value: Bytes) -> io::Result<()> {
        self.schedule(SET, key, Some(value), None)
    }

    fn delete(&self, key: &str) -> io::Result<()> {
        self.schedule(CLEAR, key, None, None)
    }

    fn sync(&self) -> BoxFuture<'_, io::Result<()>> {
        Box::pin(async { self.request(SYNC, "").await.map(drop) })
    }

    fn keys(&self) -> BoxFuture<'_, io::Result<Vec<String>>> {
        Box::pin(async move {
            if !self.lists_keys {
                return Err(io::Error::new(
                    io::ErrorKind::Unsupported,
                    "the store has no keys",
                ));
            }
            match self.request(KEYS, "").await? {
                Answer::Keys(keys) => Ok(keys),
                _ => Err(unexpected("keys")),
            }
        })
    }
}

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

/// `call`'s value if it is ready at once, polled here on the calling thread;
/// else an awaitable of it, as it goes on in the tokio runtime. Ready, a call
/// spares two thread hops: what a call costs otherwise, several times the SQL
/// it runs.
fn answer<'py, T, F>(py: Python<'py>, call: F) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = PyResult<T>> + Send + 'static,
    T: for<'a> IntoPyObject<'a> + Send + 'static,
{
    use std::task::{Context, Poll, Waker};
    let mut call = Box::pin(call);
    let poll = {
        // What the call reaches of tokio (the page cache's disk tier) finds
        // the runtime it goes on in when pending.
        let _runtime = pyo3_async_runtimes::tokio::get_runtime().enter();
        call.as_mut().poll(&mut Context::from_waker(Waker::noop()))
    };
    match poll {
        Poll::Ready(result) => result?.into_bound_py_any(py),
        Poll::Pending => pyo3_async_runtimes::tokio::future_into_py(py, call),
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
    let var = |name: &str| std::env::var(name).ok().filter(|v| !v.is_empty());
    let off = var("RESTATE_SQL_CACHE").map(|v| v.to_ascii_lowercase());
    if matches!(off.as_deref(), Some("off" | "0" | "false" | "no")) {
        return Ok(None);
    }
    let size = |name: &str, default: usize| match var(name) {
        Some(value) => state::parse_size(&value).ok_or_else(|| {
            io::Error::other(format!(
                "{name}={value}: not a size (bytes, or with KiB, MiB or GiB)"
            ))
        }),
        None => Ok(default),
    };
    Ok(Some(PageCacheConfig {
        file: std::env::var_os("RESTATE_SQL_CACHE_FILE").map(std::path::PathBuf::from),
        ..PageCacheConfig::sized(
            size("RESTATE_SQL_CACHE_MEMORY", 256 << 20)?,
            size("RESTATE_SQL_CACHE_DISK", 1 << 30)?,
        )
    }))
}

/// Open the database of `namespace` over the store behind `dispatch`, which
/// runs on `event_loop` (see the module's doc), kept warm in the process's
/// page cache; `lists_keys` if the store has `keys`. See
/// [`sql::Open::reader`] for a `reader`.
#[pyfunction]
#[pyo3(signature = (namespace, dispatch, event_loop, lists_keys = false, reader = false))]
pub(crate) fn sql_open<'py>(
    py: Python<'py>,
    namespace: String,
    dispatch: Py<PyAny>,
    event_loop: Py<PyAny>,
    lists_keys: bool,
    reader: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let store = Arc::new(PyStore {
        event_loop,
        dispatch,
        lists_keys,
    });
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let mut open = sql::Database::open(store);
        let cache = page_cache()
            .await
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        if let Some(cache) = &cache {
            open = open.cached(cache, &namespace);
        }
        if reader {
            open = open.reader();
        }
        Ok(PySqlDatabase(Arc::new(open.await.map_err(error)?)))
    })
}

/// One session of a namespace's database (see [`sql::Database`]): every SQL
/// commit is durable when its call completes. Each call returns its value, or
/// an awaitable of it (see [`answer`]).
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

    /// See [`sql::Database::close`]: an awaitable.
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let db = self.0.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
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
    Ok(())
}
