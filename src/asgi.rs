//! The ASGI bridge: a hyper `Service` that dispatches every request the
//! tunnel engine forwards into the SDK's unchanged ASGI-3 application.
//!
//! One request = one `app(scope, receive, send)` task on the Python event
//! loop. `receive` pulls one HTTP/2 DATA frame per call and, after the request
//! body ended, blocks until the stream closes (`http.disconnect`). `send`
//! streams `http.response.body` chunks to hyper with backpressure and supports
//! the `http.response.trailers` extension.

use std::convert::Infallible;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};

use bytes::Bytes;
use http::header::{HeaderName, HeaderValue};
use http::{HeaderMap, Request, Response, StatusCode};
use http_body::{Body, Frame, SizeHint};
use http_body_util::{BodyExt, Full, StreamBody};
use hyper::body::Incoming;
use hyper::service::Service;
use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};
use pyo3_async_runtimes::TaskLocals;
use restate_sdk_shared_core::tunnel::{BoxError, TunnelBody};
use tokio::sync::{mpsc, oneshot};
use tokio_stream::wrappers::ReceiverStream;
use tokio_util::sync::CancellationToken;

/// How many response frames may sit between the app and hyper. One keeps the
/// app's `send` awaiting until hyper has taken the previous chunk.
const RESPONSE_FRAME_BUFFER: usize = 1;

/// The ASGI app plus the event loop it runs on.
#[derive(Clone)]
pub struct AsgiService {
    app: Arc<Py<PyAny>>,
    locals: TaskLocals,
}

impl AsgiService {
    pub fn new(app: Py<PyAny>, locals: TaskLocals) -> Self {
        Self {
            app: Arc::new(app),
            locals,
        }
    }
}

impl Service<Request<Incoming>> for AsgiService {
    type Response = Response<TunnelBody>;
    type Error = Infallible;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn call(&self, request: Request<Incoming>) -> Self::Future {
        let app = Arc::clone(&self.app);
        let locals = self.locals.clone();
        Box::pin(async move { Ok(handle_request(app, locals, request).await) })
    }
}

async fn handle_request(
    app: Arc<Py<PyAny>>,
    locals: TaskLocals,
    request: Request<Incoming>,
) -> Response<TunnelBody> {
    let (parts, body) = request.into_parts();
    let closed = CancellationToken::new();
    // Until the response head exists, this future is the only owner of the stream:
    // hyper drops it on a reset, a lost connection or a force shutdown. Dropping it
    // must still turn `receive()` into `http.disconnect`.
    let closed_guard = closed.clone().drop_guard();
    let (head_tx, head_rx) = oneshot::channel::<ResponseHead>();
    let (frame_tx, frame_rx) =
        mpsc::channel::<Result<Frame<Bytes>, BoxError>>(RESPONSE_FRAME_BUFFER);
    let abort = Arc::new(Mutex::new(None));

    let receive = Arc::new(ReceiveState {
        body: tokio::sync::Mutex::new(Some(body)),
        ended: AtomicBool::new(false),
        closed: closed.clone(),
    });
    let send = Arc::new(SendState {
        inner: Mutex::new(SendInner {
            stage: Stage::AwaitingStart,
            head: Some(head_tx),
            frames: Some(frame_tx),
            pending_trailers: HeaderMap::new(),
        }),
        abort: Arc::clone(&abort),
        closed: closed.clone(),
    });

    // Building the scope and starting the coroutine takes the GIL: do it off the
    // runtime's worker threads, which hyper needs for the connection itself.
    let app_future = tokio::task::spawn_blocking({
        let receive = Arc::clone(&receive);
        let send = Arc::clone(&send);
        let locals = locals.clone();
        move || {
            Python::attach(|py| -> PyResult<_> {
                let scope = build_scope(py, &parts)?;
                let receive = Py::new(
                    py,
                    AsgiReceive {
                        state: receive,
                        locals: locals.clone(),
                    },
                )?;
                let send = Py::new(
                    py,
                    AsgiSend {
                        state: send,
                        locals: locals.clone(),
                    },
                )?;
                let coroutine = app.bind(py).call1((scope, receive, send))?;
                pyo3_async_runtimes::into_future_with_locals(&locals, coroutine)
            })
        }
    })
    .await;
    let app_future = match app_future {
        Ok(Ok(future)) => future,
        Ok(Err(error)) => {
            log_app_error(&error, "The ASGI application could not be started");
            return internal_error();
        }
        Err(join_error) => {
            tracing::error!(error = %join_error, "The ASGI application could not be started");
            return internal_error();
        }
    };

    // The app keeps running on the Python loop after the response head is
    // available; this task observes how it ends.
    tokio::spawn({
        let send = Arc::clone(&send);
        async move {
            match app_future.await {
                Ok(_) => {
                    tracing::debug!("ASGI application task finished");
                    send.app_finished(None)
                }
                Err(error) => {
                    let cancelled = Python::attach(|py| {
                        error.is_instance_of::<pyo3::exceptions::asyncio::CancelledError>(py)
                    });
                    if cancelled {
                        // The event loop is going away (asyncio.run exit): not an app failure.
                        tracing::debug!("ASGI application task cancelled");
                    } else {
                        log_app_error(&error, "Exception in the ASGI application");
                    }
                    send.app_finished(Some(error.to_string()));
                }
            }
        }
    });

    let head = match head_rx.await {
        Ok(head) => head,
        Err(_) => return internal_error(),
    };
    // From here on the response body owns the stream's fate.
    closed_guard.disarm();
    let mut response = Response::new(
        ClosingBody {
            inner: StreamBody::new(ReceiverStream::new(frame_rx)),
            abort,
            closed,
        }
        .boxed_unsync(),
    );
    *response.status_mut() = head.status;
    *response.headers_mut() = head.headers;
    response
}

fn log_app_error(error: &PyErr, message: &str) {
    Python::attach(|py| {
        let traceback = error
            .traceback(py)
            .and_then(|traceback| traceback.format().ok())
            .unwrap_or_default();
        tracing::error!(error = %error, "{message}\n{traceback}");
    });
}

fn internal_error() -> Response<TunnelBody> {
    let mut response = Response::new(
        Full::new(Bytes::from_static(b"Internal Server Error"))
            .map_err(|never| match never {})
            .boxed_unsync(),
    );
    *response.status_mut() = StatusCode::INTERNAL_SERVER_ERROR;
    response
}

fn build_scope<'py>(py: Python<'py>, parts: &http::request::Parts) -> PyResult<Bound<'py, PyDict>> {
    let scope = PyDict::new(py);
    scope.set_item("type", "http")?;
    let asgi = PyDict::new(py);
    asgi.set_item("version", "3.0")?;
    asgi.set_item("spec_version", "2.3")?;
    scope.set_item("asgi", asgi)?;
    scope.set_item("http_version", "2")?;
    scope.set_item("method", parts.method.as_str())?;
    scope.set_item("scheme", "https")?;
    let raw_path = parts.uri.path();
    scope.set_item(
        "path",
        percent_encoding::percent_decode_str(raw_path)
            .decode_utf8_lossy()
            .as_ref(),
    )?;
    scope.set_item("raw_path", PyBytes::new(py, raw_path.as_bytes()))?;
    scope.set_item(
        "query_string",
        PyBytes::new(py, parts.uri.query().unwrap_or("").as_bytes()),
    )?;
    scope.set_item("root_path", "")?;
    let headers = PyList::empty(py);
    for (name, value) in &parts.headers {
        headers.append(PyTuple::new(
            py,
            [
                PyBytes::new(py, name.as_str().as_bytes()),
                PyBytes::new(py, value.as_bytes()),
            ],
        )?)?;
    }
    scope.set_item("headers", headers)?;
    scope.set_item("client", py.None())?;
    scope.set_item("server", py.None())?;
    let extensions = PyDict::new(py);
    extensions.set_item("http.response.trailers", PyDict::new(py))?;
    scope.set_item("extensions", extensions)?;
    Ok(scope)
}

// ───────────────────────────── receive ─────────────────────────────

struct ReceiveState {
    body: tokio::sync::Mutex<Option<Incoming>>,
    ended: AtomicBool,
    closed: CancellationToken,
}

/// What `receive()` resolves to; converted to the ASGI dict where the Python future is resolved.
enum ReceiveEvent {
    Request { body: Bytes, more_body: bool },
    Disconnect,
}

impl<'py> IntoPyObject<'py> for ReceiveEvent {
    type Target = PyDict;
    type Output = Bound<'py, PyDict>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let event = PyDict::new(py);
        match self {
            Self::Request { body, more_body } => {
                event.set_item("type", "http.request")?;
                event.set_item("body", PyBytes::new(py, &body))?;
                event.set_item("more_body", more_body)?;
            }
            Self::Disconnect => {
                event.set_item("type", "http.disconnect")?;
            }
        }
        Ok(event)
    }
}

impl ReceiveState {
    async fn next_event(&self) -> PyResult<ReceiveEvent> {
        if !self.ended.load(Ordering::Acquire) {
            let mut guard = self.body.lock().await;
            while let Some(body) = guard.as_mut() {
                let frame = tokio::select! {
                    biased;
                    _ = self.closed.cancelled() => {
                        *guard = None;
                        self.ended.store(true, Ordering::Release);
                        return disconnect_event();
                    }
                    frame = body.frame() => frame,
                };
                match frame {
                    Some(Ok(frame)) => match frame.into_data() {
                        Ok(data) => return request_event(data, true),
                        // Request trailers carry nothing for the app; the next
                        // frame is the end of the body.
                        Err(_) => continue,
                    },
                    Some(Err(_)) => {
                        // The stream was reset by the peer.
                        *guard = None;
                        self.ended.store(true, Ordering::Release);
                        self.closed.cancel();
                        return disconnect_event();
                    }
                    None => {
                        *guard = None;
                        self.ended.store(true, Ordering::Release);
                        return request_event(Bytes::new(), false);
                    }
                }
            }
        }
        self.closed.cancelled().await;
        disconnect_event()
    }
}

fn request_event(body: Bytes, more_body: bool) -> PyResult<ReceiveEvent> {
    Ok(ReceiveEvent::Request { body, more_body })
}

fn disconnect_event() -> PyResult<ReceiveEvent> {
    Ok(ReceiveEvent::Disconnect)
}

/// The ASGI `receive` callable of one request.
#[pyclass(name = "AsgiReceive", frozen)]
struct AsgiReceive {
    state: Arc<ReceiveState>,
    locals: TaskLocals,
}

#[pymethods]
impl AsgiReceive {
    fn __call__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let state = Arc::clone(&self.state);
        pyo3_async_runtimes::tokio::future_into_py_with_locals(
            py,
            self.locals.clone(),
            async move { state.next_event().await },
        )
    }
}

// ───────────────────────────── send ─────────────────────────────

struct ResponseHead {
    status: StatusCode,
    headers: HeaderMap,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Stage {
    AwaitingStart,
    Body { trailers: bool },
    Trailers,
    Done,
}

struct SendInner {
    stage: Stage,
    head: Option<oneshot::Sender<ResponseHead>>,
    frames: Option<mpsc::Sender<Result<Frame<Bytes>, BoxError>>>,
    pending_trailers: HeaderMap,
}

struct SendState {
    inner: Mutex<SendInner>,
    /// Set when the app ends without completing the response: the body then fails
    /// (a stream reset) instead of ending cleanly, even if a frame is still buffered.
    abort: Arc<Mutex<Option<String>>>,
    closed: CancellationToken,
}

enum Message {
    Start {
        status: StatusCode,
        headers: HeaderMap,
        trailers: bool,
    },
    Body {
        body: Bytes,
        more_body: bool,
    },
    Trailers {
        headers: HeaderMap,
        more_trailers: bool,
    },
}

impl Message {
    fn parse(message: &Bound<'_, PyDict>) -> PyResult<Self> {
        let kind: String = message
            .get_item("type")?
            .ok_or_else(|| PyTypeError::new_err("ASGI message has no 'type'"))?
            .extract()?;
        match kind.as_str() {
            "http.response.start" => {
                let status: u16 = message
                    .get_item("status")?
                    .ok_or_else(|| PyTypeError::new_err("http.response.start has no 'status'"))?
                    .extract()?;
                let status = StatusCode::from_u16(status).map_err(|_| {
                    PyRuntimeError::new_err(format!("invalid HTTP status {status}"))
                })?;
                let headers = match message.get_item("headers")? {
                    Some(headers) => parse_headers(&headers)?,
                    None => HeaderMap::new(),
                };
                let trailers = match message.get_item("trailers")? {
                    Some(value) => value.extract()?,
                    None => false,
                };
                Ok(Self::Start {
                    status,
                    headers,
                    trailers,
                })
            }
            "http.response.body" => {
                let body = match message.get_item("body")? {
                    Some(body) => Bytes::copy_from_slice(body.cast::<PyBytes>()?.as_bytes()),
                    None => Bytes::new(),
                };
                let more_body = match message.get_item("more_body")? {
                    Some(value) => value.extract()?,
                    None => false,
                };
                Ok(Self::Body { body, more_body })
            }
            "http.response.trailers" => {
                let headers = match message.get_item("headers")? {
                    Some(headers) => parse_headers(&headers)?,
                    None => HeaderMap::new(),
                };
                let more_trailers = match message.get_item("more_trailers")? {
                    Some(value) => value.extract()?,
                    None => false,
                };
                Ok(Self::Trailers {
                    headers,
                    more_trailers,
                })
            }
            other => Err(PyRuntimeError::new_err(format!(
                "Unexpected ASGI message type '{other}'"
            ))),
        }
    }

    fn kind(&self) -> &'static str {
        match self {
            Self::Start { .. } => "http.response.start",
            Self::Body { .. } => "http.response.body",
            Self::Trailers { .. } => "http.response.trailers",
        }
    }
}

fn parse_headers(headers: &Bound<'_, PyAny>) -> PyResult<HeaderMap> {
    let mut map = HeaderMap::new();
    for pair in headers.try_iter()? {
        let pair = pair?;
        let name = pair.get_item(0)?;
        let value = pair.get_item(1)?;
        let name = HeaderName::from_bytes(name.cast::<PyBytes>()?.as_bytes())
            .map_err(|error| PyRuntimeError::new_err(format!("invalid header name: {error}")))?;
        let value = HeaderValue::from_bytes(value.cast::<PyBytes>()?.as_bytes())
            .map_err(|error| PyRuntimeError::new_err(format!("invalid header value: {error}")))?;
        map.append(name, value);
    }
    Ok(map)
}

impl SendState {
    async fn send(&self, message: Message) -> PyResult<()> {
        // The message must fit the protocol state even after a disconnection; what
        // is valid is then delivered or, with nobody left to deliver to, dropped so
        // the app can finish.
        let step = {
            let mut inner = self.inner.lock().expect("send state lock poisoned");
            inner.step(message)?
        };
        if self.closed.is_cancelled() {
            return Ok(());
        }
        match step {
            Step::Nothing => Ok(()),
            Step::Frame {
                sender,
                frame,
                last,
            } => {
                // Backpressure: wait until hyper took the previous frame. A closed
                // receiver means the stream is gone; the app may finish quietly.
                if let Ok(permit) = sender.reserve().await {
                    permit.send(Ok(frame));
                }
                if last {
                    drop(sender);
                }
                Ok(())
            }
        }
    }

    fn app_finished(&self, error: Option<String>) {
        let mut inner = self.inner.lock().expect("send state lock poisoned");
        match inner.stage {
            Stage::AwaitingStart => {
                // The app returned (or failed) without a response.
                if let Some(head) = inner.head.take() {
                    let _ = head.send(ResponseHead {
                        status: StatusCode::INTERNAL_SERVER_ERROR,
                        headers: HeaderMap::new(),
                    });
                }
                inner.frames.take();
                inner.stage = Stage::Done;
            }
            Stage::Body { .. } | Stage::Trailers => {
                // An incomplete response: reset the stream rather than ending it cleanly.
                // Recorded beside the channel, so a frame still buffered in it cannot
                // turn the failure into a clean end of stream.
                let reason = error.unwrap_or_else(|| {
                    "the ASGI application finished without completing the response".to_owned()
                });
                *self.abort.lock().expect("abort lock poisoned") = Some(reason);
                inner.frames.take();
                inner.stage = Stage::Done;
            }
            Stage::Done => {}
        }
    }
}

enum Step {
    Nothing,
    Frame {
        sender: mpsc::Sender<Result<Frame<Bytes>, BoxError>>,
        frame: Frame<Bytes>,
        last: bool,
    },
}

impl SendInner {
    fn step(&mut self, message: Message) -> PyResult<Step> {
        match (self.stage, message) {
            (
                Stage::AwaitingStart,
                Message::Start {
                    status,
                    headers,
                    trailers,
                },
            ) => {
                if let Some(head) = self.head.take() {
                    let _ = head.send(ResponseHead { status, headers });
                }
                self.stage = Stage::Body { trailers };
                Ok(Step::Nothing)
            }
            (Stage::Body { trailers }, Message::Body { body, more_body }) => {
                let Some(sender) = self.frames.clone() else {
                    return Ok(Step::Nothing);
                };
                let last = !more_body && !trailers;
                if !more_body {
                    self.stage = if trailers {
                        Stage::Trailers
                    } else {
                        Stage::Done
                    };
                }
                if last {
                    self.frames.take();
                }
                Ok(Step::Frame {
                    sender,
                    frame: Frame::data(body),
                    last,
                })
            }
            (
                Stage::Trailers,
                Message::Trailers {
                    headers,
                    more_trailers,
                },
            ) => {
                for (name, value) in headers {
                    if let Some(name) = name {
                        self.pending_trailers.append(name, value);
                    }
                }
                if more_trailers {
                    return Ok(Step::Nothing);
                }
                let Some(sender) = self.frames.take() else {
                    return Ok(Step::Nothing);
                };
                self.stage = Stage::Done;
                Ok(Step::Frame {
                    sender,
                    frame: Frame::trailers(std::mem::take(&mut self.pending_trailers)),
                    last: true,
                })
            }
            (stage, message) => {
                let expected = match stage {
                    Stage::AwaitingStart => "'http.response.start'",
                    Stage::Body { .. } => "'http.response.body'",
                    Stage::Trailers => "'http.response.trailers'",
                    Stage::Done => "no further message (the response is complete)",
                };
                Err(PyRuntimeError::new_err(format!(
                    "Unexpected ASGI message '{}', expected {expected}",
                    message.kind()
                )))
            }
        }
    }
}

/// The ASGI `send` callable of one request.
#[pyclass(name = "AsgiSend", frozen)]
struct AsgiSend {
    state: Arc<SendState>,
    locals: TaskLocals,
}

#[pymethods]
impl AsgiSend {
    fn __call__<'py>(
        &self,
        py: Python<'py>,
        message: &Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let message = Message::parse(message)?;
        let state = Arc::clone(&self.state);
        pyo3_async_runtimes::tokio::future_into_py_with_locals(
            py,
            self.locals.clone(),
            async move { state.send(message).await },
        )
    }
}

/// The response body handed to hyper. Ending or dropping it (hyper drops the
/// body on a stream reset) marks the stream closed, which turns `receive` into
/// `http.disconnect` and makes later `send`s no-ops.
struct ClosingBody {
    inner: StreamBody<ReceiverStream<Result<Frame<Bytes>, BoxError>>>,
    abort: Arc<Mutex<Option<String>>>,
    closed: CancellationToken,
}

impl Body for ClosingBody {
    type Data = Bytes;
    type Error = BoxError;

    fn poll_frame(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
        let this = self.get_mut();
        let mut result = Pin::new(&mut this.inner).poll_frame(cx);
        if matches!(result, Poll::Ready(None)) {
            if let Some(reason) = this.abort.lock().expect("abort lock poisoned").take() {
                result = Poll::Ready(Some(Err(reason.into())));
            }
        }
        if matches!(result, Poll::Ready(None) | Poll::Ready(Some(Err(_)))) {
            this.closed.cancel();
        }
        result
    }

    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }

    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
}

impl Drop for ClosingBody {
    fn drop(&mut self) {
        self.closed.cancel();
    }
}
