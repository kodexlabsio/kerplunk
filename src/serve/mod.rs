//! `konstruct serve`: the graph viewer and a read-only JSON API over the graph, on HTTP/1.1.
//!
//! The routes are fixed, so no request names a file and no path can leave the output directory:
//!
//! | Route | Answer |
//! |-------|--------|
//! | `/` | The viewer, with `<meta name="konstruct-serve">` so it loads the files below |
//! | `/graph.json`, `/explain.json` | The build's files, with strong ETags, gzip and brotli made once per version |
//! | `/events` | Server-sent events, unnamed: one on connecting and one each time the graph changes |
//! | `/api/v1/...` | The operations in [`api::OPERATIONS`], each answered by the [`Backend`] (Python) |
//! | `/api/v1/openapi.json` | The OpenAPI document made from the same table |
//!
//! The server answers nothing from the graph itself. It routes, checks parameters, pages lists and
//! keeps the answers that depend only on the graph until the graph changes, so the HTTP API, the
//! command line and the MCP server share one implementation of every answer.
//!
//! `graph.json` is checked for changes every [`POLL`]. A build replaces it by renaming a complete
//! file into place, so it is never read half written; a changed file is read, checked and
//! compressed off the request path and swapped in whole. Polling one file's metadata costs a
//! system call per interval and behaves the same on every platform and file system (network
//! shares and container mounts included, where change notification is unreliable), so it needs no
//! watcher thread or dependency.
//!
//! Requests are checked before they are routed: only GET and HEAD, and only a `Host` naming the
//! server (a loopback or other IP address, `localhost` or the host it was bound to, with its
//! port), which stops DNS rebinding; a request with two `Host` headers is refused. Headers are
//! capped in size and number, connections and event streams in number (streams lower, so they
//! cannot hold every connection), and a client that does not finish its headers in time is
//! disconnected.

pub mod api;
pub mod store;

use std::collections::HashMap;
use std::convert::Infallible;
use std::io::{self, Write as _};
use std::net::{IpAddr, SocketAddr, TcpListener as StdListener, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::{Arc, Mutex, OnceLock, RwLock};
use std::task::{Context, Poll};
use std::time::{Duration, SystemTime};

use bytes::Bytes;
use http_body_util::{Either, Full};
use hyper::body::{Body, Frame, Incoming};
use hyper::header::{self, HeaderMap, HeaderName, HeaderValue};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::{TokioIo, TokioTimer};
use hyper_util::server::graceful::GracefulShutdown;
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio::sync::{Semaphore, mpsc, watch};

use serde_json::Value;

use self::api::{Argument, Operation, Page};
use self::store::GraphStore;
use crate::fnv;

/// How often `graph.json` is checked for a new version.
pub const POLL: Duration = Duration::from_millis(250);
/// How often an event stream with nothing to say sends a comment, so a dead client is noticed.
const HEARTBEAT: Duration = Duration::from_secs(20);
/// How long connections have to finish once the server is stopping.
const GRACE: Duration = Duration::from_secs(2);
/// Answers larger than this are gzipped when the client accepts it.
const COMPRESS_ABOVE: usize = 64 * 1024;
const MAX_HEADER_BYTES: usize = 16 * 1024;
const MAX_HEADERS: usize = 64;
/// Answers kept per graph version before the cache starts again.
const CACHE_ENTRIES: usize = 1024;
const TOKEN_COOKIE: &str = "konstruct_token";

/// The graph an operation is asked of: the one the server serves, as the bytes of its
/// `graph.json` and their [`graph_version`]. The backend answers from these, never from the file
/// on disk, so an answer and everything else the server sends describe one graph, and a file the
/// server refused (invalid JSON, a newer format) is never read behind its back.
#[derive(Clone, Debug)]
pub struct Served {
    pub version: String,
    pub bytes: Bytes,
}

/// What a Python-backed operation answers, about the graph it was given.
#[derive(Debug)]
pub enum Outcome {
    Answer {
        media: String,
        body: Vec<u8>,
    },
    /// A problem type from [`api::PROBLEM_TYPES`], and what went wrong.
    Problem {
        kind: String,
        detail: String,
    },
}

/// Answers the operations that are written in Python. It is called on a blocking thread, one
/// call per request that is not cached.
pub trait Backend: Send + Sync + 'static {
    fn answer(
        &self,
        graph: &Served,
        operation: &str,
        arguments: &[(&'static str, Argument)],
    ) -> Outcome;
}

/// The version of a file the server sends, `graph.json` above all: FNV-1a 64 of its bytes, in
/// hexadecimal. The backend is given the graph it answers about with the same version.
pub fn graph_version(bytes: &[u8]) -> String {
    format!("{:016x}", fnv::hash(fnv::OFFSET_BASIS, bytes))
}

/// How a server is set up; [`Options::new`] gives the defaults.
#[derive(Clone)]
pub struct Options {
    /// The output directory holding `graph.json` and `explain.json`.
    pub out_dir: PathBuf,
    pub host: String,
    /// 0 for any free port.
    pub port: u16,
    /// The viewer page to serve at `/`.
    pub page: String,
    pub max_connections: usize,
    /// Event streams open at once; each holds its connection for as long as the page is open,
    /// so they have a smaller cap of their own and cannot take every connection.
    pub max_event_streams: usize,
    /// Answers that are not kept, waiting for Python at once. Python answers one at a time, so
    /// a longer queue only makes each wait longer; past it the server answers `busy`.
    pub max_queued_answers: usize,
    /// How long a client has to send a request's headers.
    pub header_timeout: Duration,
    pub poll: Duration,
    /// The bearer token every request needs, when there is one.
    pub token: Option<String>,
    pub backend: Option<Arc<dyn Backend>>,
}

impl std::fmt::Debug for Options {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Options")
            .field("out_dir", &self.out_dir)
            .field("host", &self.host)
            .field("port", &self.port)
            .finish_non_exhaustive()
    }
}

impl Options {
    pub fn new(out_dir: impl Into<PathBuf>, host: &str, port: u16, page: String) -> Self {
        Self {
            out_dir: out_dir.into(),
            host: host.to_owned(),
            port,
            page,
            max_connections: 256,
            max_event_streams: 64,
            max_queued_answers: 8,
            header_timeout: Duration::from_secs(10),
            poll: POLL,
            token: None,
            backend: None,
        }
    }
}

/// A content coding the server can send.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Encoding {
    Identity,
    Gzip,
    Brotli,
}

impl Encoding {
    fn name(self) -> Option<&'static str> {
        match self {
            Self::Identity => None,
            Self::Gzip => Some("gzip"),
            Self::Brotli => Some("br"),
        }
    }
}

/// The coding to send for an `Accept-Encoding` header: brotli, then gzip, as the client's
/// q-values allow, preferring brotli when they are equal.
pub fn negotiate(accept: Option<&str>, brotli: bool) -> Encoding {
    let Some(accept) = accept else {
        return Encoding::Identity;
    };
    let mut gzip_q: Option<f32> = None;
    let mut br_q: Option<f32> = None;
    let mut any_q: Option<f32> = None;
    for part in accept.split(',') {
        let mut pieces = part.split(';');
        let name = pieces.next().unwrap_or("").trim().to_ascii_lowercase();
        let mut q = 1.0f32;
        for parameter in pieces {
            if let Some((key, value)) = parameter.split_once('=')
                && key.trim().eq_ignore_ascii_case("q")
            {
                q = value.trim().parse().unwrap_or(0.0);
            }
        }
        match name.as_str() {
            "gzip" | "x-gzip" => gzip_q = Some(q),
            "br" => br_q = Some(q),
            "*" => any_q = Some(q),
            _ => {}
        }
    }
    let br = if brotli {
        br_q.or(any_q).unwrap_or(0.0)
    } else {
        0.0
    };
    let gzip = gzip_q.or(any_q).unwrap_or(0.0);
    if br > 0.0 && br >= gzip {
        Encoding::Brotli
    } else if gzip > 0.0 {
        Encoding::Gzip
    } else {
        Encoding::Identity
    }
}

/// Whether an `If-None-Match` header names `etag` (weak comparison, as RFC 9110 asks for it).
pub fn not_modified(if_none_match: Option<&str>, etag: &str) -> bool {
    let Some(header) = if_none_match else {
        return false;
    };
    header
        .split(',')
        .map(str::trim)
        .any(|tag| tag == "*" || tag.strip_prefix("W/").unwrap_or(tag) == etag)
}

/// One unnamed server-sent event, which an `EventSource`'s `onmessage` receives (a named one
/// reaches only listeners for its name). Each line of `data` becomes a `data:` line, so a
/// newline cannot end the event early.
pub fn sse_message(id: &str, data: &str) -> String {
    let mut text = String::with_capacity(data.len() + 32);
    text.push_str("id: ");
    text.push_str(&id.replace(['\r', '\n'], ""));
    text.push('\n');
    for line in data.split('\n') {
        text.push_str("data: ");
        text.push_str(line.strip_suffix('\r').unwrap_or(line));
        text.push('\n');
    }
    text.push('\n');
    text
}

/// The `Host` values a request may carry: DNS rebinding sends a name the attacker controls,
/// which is never an IP address, `localhost` or the host the server was bound to.
#[derive(Clone, Debug)]
pub struct HostCheck {
    port: u16,
    names: Vec<String>,
}

impl HostCheck {
    pub fn new(host: &str, port: u16) -> Self {
        let mut names = vec!["localhost".to_owned()];
        let host = host.trim_start_matches('[').trim_end_matches(']');
        let host = host.trim_end_matches('.').to_ascii_lowercase();
        if !host.is_empty() && host.parse::<IpAddr>().is_err() {
            names.push(host);
        }
        Self { port, names }
    }

    pub fn allows(&self, header: Option<&str>) -> bool {
        let Some(header) = header.map(str::trim) else {
            return false;
        };
        let (name, port) = if let Some(rest) = header.strip_prefix('[') {
            let Some((address, after)) = rest.split_once(']') else {
                return false;
            };
            if address.parse::<std::net::Ipv6Addr>().is_err() {
                return false;
            }
            match after {
                "" => (address.to_owned(), None),
                _ => match after.strip_prefix(':') {
                    Some(port) => (address.to_owned(), Some(port)),
                    None => return false,
                },
            }
        } else {
            match header.split_once(':') {
                Some((name, port)) => (name.to_ascii_lowercase(), Some(port)),
                None => (header.to_ascii_lowercase(), None),
            }
        };
        let port = match port {
            None => 80,
            Some(port) => match port.parse::<u16>() {
                Ok(port) => port,
                Err(_) => return false,
            },
        };
        if port != self.port {
            return false;
        }
        let name = name.trim_end_matches('.');
        name.parse::<IpAddr>().is_ok() || self.names.iter().any(|n| n == name)
    }
}

/// What a request asks for.
#[derive(Debug, PartialEq, Eq)]
pub enum Route<'a> {
    Page,
    Graph,
    Explain,
    Events,
    OpenApi,
    /// An operation of the API, with its path parameter still percent-encoded.
    Api(&'static Operation, Option<&'a str>),
    NotFound,
}

/// Decode `%XX` escapes (and, in a query, `+` as a space); `None` for a bad escape or text that
/// is not UTF-8.
pub fn percent_decode(text: &str, plus_is_space: bool) -> Option<String> {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut position = 0;
    while position < bytes.len() {
        match bytes[position] {
            b'%' => {
                let hex = bytes.get(position + 1..position + 3)?;
                if !hex.iter().all(u8::is_ascii_hexdigit) {
                    return None;
                }
                let digits = std::str::from_utf8(hex).ok()?;
                out.push(u8::from_str_radix(digits, 16).ok()?);
                position += 3;
            }
            b'+' if plus_is_space => {
                out.push(b' ');
                position += 1;
            }
            byte => {
                out.push(byte);
                position += 1;
            }
        }
    }
    String::from_utf8(out).ok()
}

/// The route for a request path, which is matched whole: there is no mapping onto files.
pub fn route(path: &str) -> Route<'_> {
    match path {
        "/" | "/index.html" => Route::Page,
        "/graph.json" => Route::Graph,
        "/explain.json" => Route::Explain,
        "/events" => Route::Events,
        "/api/v1/openapi.json" => Route::OpenApi,
        _ => match api::find(path) {
            Some((operation, value)) => Route::Api(operation, value),
            None => Route::NotFound,
        },
    }
}

/// Whether a browser sent the request for a page on another site: `Sec-Fetch-Site` says so, or
/// `Origin` names an origin other than this server's (`null` included). Clients that are not
/// browsers send neither, and same-origin `GET`s from the page send no `Origin`.
pub fn from_another_site(request: &Request<Incoming>, hosts: &HostCheck) -> bool {
    let site = text(request, HeaderName::from_static("sec-fetch-site"));
    if site.is_some_and(|site| !matches!(site, "same-origin" | "none")) {
        return true;
    }
    match text(request, header::ORIGIN) {
        None => false,
        Some(origin) => !origin
            .strip_prefix("http://")
            .is_some_and(|authority| hosts.allows(Some(authority))),
    }
}

/// Whether two tokens are equal, taking the same time wherever they differ.
pub fn same_token(given: &[u8], expected: &[u8]) -> bool {
    let mut difference = given.len() ^ expected.len();
    for (index, &byte) in expected.iter().enumerate() {
        difference |= usize::from(byte ^ given.get(index).copied().unwrap_or(!byte));
    }
    difference == 0
}

/// The token a request carries: a bearer token, the cookie the page sets, or, for the page
/// itself, `?token=`.
fn presented<'a>(
    headers: &'a HeaderMap,
    pairs: &'a [(String, String)],
    page: bool,
) -> Vec<&'a str> {
    let mut found = Vec::new();
    if let Some(value) = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        && let Some((scheme, token)) = value.split_once(' ')
        && scheme.eq_ignore_ascii_case("bearer")
    {
        found.push(token.trim());
    }
    for cookies in headers.get_all(header::COOKIE) {
        let Ok(cookies) = cookies.to_str() else {
            continue;
        };
        for cookie in cookies.split(';') {
            if let Some((name, value)) = cookie.trim().split_once('=')
                && name == TOKEN_COOKIE
            {
                found.push(value);
            }
        }
    }
    if page {
        found.extend(
            pairs
                .iter()
                .filter(|(k, _)| k == "token")
                .map(|(_, v)| v.as_str()),
        );
    }
    found
}

/// A query string's parameters, decoded; `None` when one is not valid.
pub fn query_pairs(query: Option<&str>) -> Option<Vec<(String, String)>> {
    let Some(query) = query else {
        return Some(Vec::new());
    };
    query
        .split('&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| {
            let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
            Some((percent_decode(key, true)?, percent_decode(value, true)?))
        })
        .collect()
}

fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        for (index, shift) in [18u32, 12, 6, 0].into_iter().enumerate() {
            if index <= chunk.len() {
                out.push(ALPHABET[((n >> shift) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// The contents of each `<tag>` element in `page` (scripts that are not JSON data blocks, or
/// styles), for the hashes a Content-Security-Policy allows them by.
fn inline(page: &str, tag: &str) -> Vec<String> {
    let open = format!("<{tag}");
    let close = format!("</{tag}>");
    let mut found = Vec::new();
    let mut rest = page;
    while let Some(start) = rest.find(&open) {
        let after = &rest[start + open.len()..];
        let Some(end_of_tag) = after.find('>') else {
            break;
        };
        let attributes = &after[..end_of_tag];
        let body = &after[end_of_tag + 1..];
        let Some(end) = body.find(&close) else { break };
        if !attributes.contains("application/json") && !attributes.contains("src=") {
            found.push(body[..end].to_owned());
        }
        rest = &body[end + close.len()..];
    }
    found
}

fn sources(page: &str, tag: &str) -> String {
    inline(page, tag)
        .iter()
        .map(|text| format!("'sha256-{}'", base64(&Sha256::digest(text.as_bytes()))))
        .collect::<Vec<_>>()
        .join(" ")
}

/// The page's policy: its own inline script and styles by hash, fonts and images from data
/// URIs, requests only to this server, and nothing else.
pub fn content_security_policy(page: &str) -> String {
    let scripts = sources(page, "script");
    let styles = sources(page, "style");
    format!(
        "default-src 'none'; script-src {scripts}; style-src {styles}; img-src data:; \
         font-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; \
         frame-ancestors 'none'"
    )
    .replace("script-src ;", "script-src 'none';")
    .replace("style-src ;", "style-src 'none';")
}

/// One file the server sends, in each coding. The codings are made once, on a thread of their
/// own, so the file is served (uncompressed) from the moment it is read.
#[derive(Debug)]
struct Asset {
    identity: Bytes,
    /// gzip and brotli, once they are made.
    encoded: OnceLock<(Bytes, Bytes)>,
    version: String,
    content_type: &'static str,
}

fn gzip(bytes: &[u8], level: u32) -> Bytes {
    let mut encoder = flate2::write::GzEncoder::new(
        Vec::with_capacity(bytes.len() / 8),
        flate2::Compression::new(level),
    );
    // Writing to a Vec cannot fail.
    let _ = encoder.write_all(bytes);
    Bytes::from(encoder.finish().unwrap_or_default())
}

fn brotli(bytes: &[u8]) -> Bytes {
    let params = brotli::enc::BrotliEncoderParams {
        // Quality 5 compresses a large graph a little better than gzip's best in a fraction of
        // the time; higher qualities take seconds for little more.
        quality: 5,
        lgwin: 22,
        ..Default::default()
    };
    let mut out = Vec::with_capacity(bytes.len() / 10);
    let _ = brotli::BrotliCompress(&mut &bytes[..], &mut out, &params);
    Bytes::from(out)
}

impl Asset {
    fn new(identity: Bytes, content_type: &'static str) -> Arc<Self> {
        let version = graph_version(&identity);
        let asset = Arc::new(Self {
            identity,
            encoded: OnceLock::new(),
            version,
            content_type,
        });
        let worker = asset.clone();
        let spawned = std::thread::Builder::new()
            .name("konstruct-compress".to_owned())
            .spawn(move || worker.compress());
        if spawned.is_err() {
            asset.compress();
        }
        asset
    }

    fn compress(&self) {
        let (gzipped, brotlied) = std::thread::scope(|scope| {
            let zipped = scope.spawn(|| gzip(&self.identity, 6));
            let brotlied = brotli(&self.identity);
            (zipped.join().unwrap_or_default(), brotlied)
        });
        let _ = self.encoded.set((gzipped, brotlied));
    }

    /// `wanted`, if it has been made yet; otherwise the file as it is.
    fn available(&self, wanted: Encoding) -> Encoding {
        match (wanted, self.encoded.get()) {
            (Encoding::Identity, _) | (_, None) => Encoding::Identity,
            (encoding, Some(_)) => encoding,
        }
    }

    fn etag(&self, encoding: Encoding) -> String {
        match encoding.name() {
            None => format!("\"{}\"", self.version),
            Some(name) => format!("\"{}-{name}\"", self.version),
        }
    }

    fn body(&self, encoding: Encoding) -> &Bytes {
        match (encoding, self.encoded.get()) {
            (Encoding::Gzip, Some((gzipped, _))) => gzipped,
            (Encoding::Brotli, Some((_, brotlied))) => brotlied,
            _ => &self.identity,
        }
    }
}

/// One version of the graph and everything made from it.
#[derive(Debug)]
struct Snapshot {
    graph: GraphStore,
    file: Arc<Asset>,
    explain: Arc<Asset>,
    /// graph.json's and explain.json's identities when they were read, taken first, so a file
    /// replaced while it was being read is read again.
    stamp: (Option<Stamp>, Option<Stamp>),
}

/// The identity of a file on disk, cheap to read: a new file renamed into place differs.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Stamp {
    length: u64,
    modified: Option<SystemTime>,
    #[cfg(unix)]
    inode: u64,
}

fn stamp(path: &Path) -> Option<Stamp> {
    let metadata = std::fs::metadata(path).ok()?;
    Some(Stamp {
        length: metadata.len(),
        modified: metadata.modified().ok(),
        #[cfg(unix)]
        inode: std::os::unix::fs::MetadataExt::ino(&metadata),
    })
}

fn invalid(message: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

/// Read and index the graph in `out_dir`, compressing its files while it is indexed.
fn load(out_dir: &Path) -> io::Result<Snapshot> {
    let path = out_dir.join("graph.json");
    let read_stamp = (stamp(&path), stamp(&out_dir.join("explain.json")));
    let bytes = std::fs::read(&path).map_err(|error| match error.kind() {
        io::ErrorKind::NotFound => invalid(format!(
            "no graph at {}; run `konstruct build` first",
            path.display()
        )),
        _ => error,
    })?;
    let explain = match std::fs::read(out_dir.join("explain.json")) {
        Ok(bytes) => bytes,
        Err(_) => b"{}".to_vec(),
    };
    let graph =
        GraphStore::load(bytes).map_err(|error| invalid(format!("{}: {error}", path.display())))?;
    // The same bytes, shared, so the server holds the graph once.
    let file = Asset::new(graph.bytes.clone(), "application/json");
    Ok(Snapshot {
        graph,
        file,
        explain: Asset::new(Bytes::from(explain), "application/json"),
        stamp: read_stamp,
    })
}

/// What every connection shares.
struct Shared {
    snapshot: RwLock<Arc<Snapshot>>,
    page: Arc<Asset>,
    policy: HeaderValue,
    hosts: HostCheck,
    /// The current graph version, which each event stream follows.
    versions: watch::Sender<String>,
    stopping: watch::Sender<bool>,
    connections: Arc<Semaphore>,
    streams: Arc<Semaphore>,
    answering: Arc<Semaphore>,
    token: Option<String>,
    backend: Option<Arc<dyn Backend>>,
    /// Python's answers for one graph version, by operation and arguments.
    cache: Mutex<Cache>,
    openapi: Bytes,
}

impl std::fmt::Debug for Shared {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Shared").finish_non_exhaustive()
    }
}

/// A Python answer as it is kept: its media type, its body, and, for a list, the body parsed so
/// pages can be cut from it.
#[derive(Debug)]
struct Cached {
    /// The version of the graph the answer came from, which its cursors name.
    version: String,
    media: String,
    body: Bytes,
    parsed: Option<Value>,
    /// The body gzipped, once a client that accepts gzip has asked for a large one.
    gzipped: OnceLock<Bytes>,
}

#[derive(Debug, Default)]
struct Cache {
    version: String,
    entries: HashMap<String, Arc<Cached>>,
}

impl Shared {
    fn current(&self) -> Arc<Snapshot> {
        // A poisoned lock only means a reader panicked; the snapshot it guards is still whole.
        self.snapshot
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }

    fn replace(&self, snapshot: Snapshot) {
        let version = snapshot.file.version.clone();
        *self
            .snapshot
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Arc::new(snapshot);
        self.versions.send_replace(version);
    }
}

/// A server bound to its address with the graph loaded, ready to [`run`](Server::run).
pub struct Server {
    runtime: tokio::runtime::Runtime,
    listener: StdListener,
    address: SocketAddr,
    shared: Arc<Shared>,
    options: Options,
}

impl std::fmt::Debug for Server {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Server")
            .field("address", &self.address)
            .finish_non_exhaustive()
    }
}

impl Server {
    /// Load the graph and bind the address: the errors a user can act on (no graph, a port in
    /// use) come from here, before anything is served.
    pub fn bind(options: Options) -> io::Result<Self> {
        let snapshot = load(&options.out_dir)?;
        if let Some(token) = &options.token
            && (token.len() < 16 || !token.bytes().all(|b| b.is_ascii_graphic()))
        {
            return Err(invalid(
                "the token must be at least 16 printable characters, with no spaces".to_owned(),
            ));
        }
        let address = (options.host.as_str(), options.port)
            .to_socket_addrs()?
            .next()
            .ok_or_else(|| invalid(format!("{} is not an address", options.host)))?;
        let listener = StdListener::bind(address).map_err(|error| match error.kind() {
            io::ErrorKind::AddrInUse => io::Error::new(
                error.kind(),
                format!(
                    "port {} on {} is in use; choose another with --port, or --port 0 for any free one",
                    options.port, options.host
                ),
            ),
            _ => error,
        })?;
        listener.set_nonblocking(true)?;
        let address = listener.local_addr()?;
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .thread_name("konstruct-serve")
            .build()?;
        let page = Asset::new(
            Bytes::from(options.page.clone()),
            "text/html; charset=utf-8",
        );
        let policy = HeaderValue::from_str(&content_security_policy(&options.page))
            .map_err(|error| invalid(error.to_string()))?;
        let (versions, _) = watch::channel(snapshot.file.version.clone());
        let (stopping, _) = watch::channel(false);
        let shared = Arc::new(Shared {
            snapshot: RwLock::new(Arc::new(snapshot)),
            page,
            policy,
            hosts: HostCheck::new(&options.host, address.port()),
            versions,
            stopping,
            connections: Arc::new(Semaphore::new(options.max_connections)),
            streams: Arc::new(Semaphore::new(options.max_event_streams)),
            answering: Arc::new(Semaphore::new(options.max_queued_answers)),
            token: options.token.clone(),
            backend: options.backend.clone(),
            cache: Mutex::new(Cache::default()),
            openapi: Bytes::from(api::openapi_text()),
        });
        Ok(Self {
            runtime,
            listener,
            address,
            shared,
            options,
        })
    }

    pub fn address(&self) -> SocketAddr {
        self.address
    }

    /// Serve until `stop` returns true; it is called every 100 ms on the calling thread, which
    /// does nothing else, so it may take a lock (Python's, to check for Ctrl-C).
    pub fn run(self, mut stop: impl FnMut() -> bool) -> io::Result<()> {
        let Self {
            runtime,
            listener,
            shared,
            options,
            ..
        } = self;
        let result = runtime.block_on(async {
            let listener = TcpListener::from_std(listener)?;
            let accepting = tokio::spawn(accept(listener, shared.clone(), options.header_timeout));
            let watching = tokio::spawn(watch_graph(
                shared.clone(),
                options.out_dir.clone(),
                options.poll,
            ));
            let mut tick = tokio::time::interval(Duration::from_millis(100));
            loop {
                tick.tick().await;
                if stop() {
                    break;
                }
            }
            shared.stopping.send_replace(true);
            watching.abort();
            let _ = tokio::time::timeout(GRACE + Duration::from_millis(500), accepting).await;
            Ok::<(), io::Error>(())
        });
        runtime.shutdown_timeout(Duration::from_secs(1));
        result
    }
}

async fn accept(listener: TcpListener, shared: Arc<Shared>, header_timeout: Duration) {
    let mut builder = http1::Builder::new();
    builder
        .timer(TokioTimer::new())
        .header_read_timeout(header_timeout)
        .max_buf_size(MAX_HEADER_BYTES.max(8192))
        .max_header_size(MAX_HEADER_BYTES)
        .max_headers(MAX_HEADERS)
        .keep_alive(true);
    let graceful = GracefulShutdown::new();
    let mut stopping = shared.stopping.subscribe();
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let Ok((stream, _)) = accepted else {
                    // Out of file descriptors, say: let connections close before trying again.
                    tokio::time::sleep(Duration::from_millis(50)).await;
                    continue;
                };
                let Ok(permit) = shared.connections.clone().try_acquire_owned() else {
                    // Over the connection limit: close at once rather than queue.
                    drop(stream);
                    continue;
                };
                let _ = stream.set_nodelay(true);
                let state = shared.clone();
                let service = service_fn(move |request: Request<Incoming>| {
                    let state = state.clone();
                    async move { Ok::<_, Infallible>(respond(state, request).await) }
                });
                let connection = graceful.watch(builder.serve_connection(TokioIo::new(stream), service));
                tokio::spawn(async move {
                    let _ = connection.await;
                    drop(permit);
                });
            }
            _ = stopping.changed() => break,
        }
    }
    drop(listener);
    let _ = tokio::time::timeout(GRACE, graceful.shutdown()).await;
}

/// Check `graph.json` and `explain.json` every `poll`, and load each new version off the request
/// path: `konstruct conflicts` and `konstruct verify` rewrite explain.json, the first without
/// touching graph.json.
async fn watch_graph(shared: Arc<Shared>, out_dir: PathBuf, poll: Duration) {
    let (path, explain) = (out_dir.join("graph.json"), out_dir.join("explain.json"));
    let mut seen = shared.current().stamp.clone();
    let mut tick = tokio::time::interval(poll);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tick.tick().await;
        let now = (stamp(&path), stamp(&explain));
        if now.0.is_none() || now == seen {
            continue;
        }
        seen = now;
        let directory = out_dir.clone();
        match tokio::task::spawn_blocking(move || load(&directory)).await {
            Ok(Ok(snapshot)) => {
                let current = shared.current();
                if snapshot.file.version != current.file.version
                    || snapshot.explain.version != current.explain.version
                {
                    shared.replace(snapshot);
                }
            }
            Ok(Err(error)) => eprintln!("konstruct: keeping the previous graph: {error}"),
            Err(_) => {}
        }
    }
}

type ResponseBody = Either<Full<Bytes>, EventBody>;

/// An event stream's body: what its task sends, as it sends it.
#[derive(Debug)]
pub struct EventBody {
    receiver: mpsc::Receiver<Bytes>,
}

impl Body for EventBody {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        self.receiver
            .poll_recv(context)
            .map(|chunk| chunk.map(|bytes| Ok(Frame::data(bytes))))
    }
}

fn header(name: HeaderName, value: &'static str) -> (HeaderName, HeaderValue) {
    (name, HeaderValue::from_static(value))
}

/// Headers on every response.
fn secure(headers: &mut HeaderMap, policy: Option<&HeaderValue>) {
    let fixed = [
        header(header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        header(header::REFERRER_POLICY, "no-referrer"),
        header(header::X_FRAME_OPTIONS, "DENY"),
        header(
            HeaderName::from_static("cross-origin-resource-policy"),
            "same-origin",
        ),
        header(
            HeaderName::from_static("cross-origin-opener-policy"),
            "same-origin",
        ),
    ];
    headers.extend(fixed);
    headers.insert(
        header::CONTENT_SECURITY_POLICY,
        policy.cloned().unwrap_or_else(|| {
            HeaderValue::from_static("default-src 'none'; frame-ancestors 'none'")
        }),
    );
}

fn reply(status: StatusCode, content_type: &str, body: Bytes) -> Response<ResponseBody> {
    let mut response = Response::new(Either::Left(Full::new(body)));
    *response.status_mut() = status;
    let headers = response.headers_mut();
    let media = HeaderValue::from_str(content_type)
        .unwrap_or_else(|_| HeaderValue::from_static("application/octet-stream"));
    headers.insert(header::CONTENT_TYPE, media);
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

/// An RFC 9457 problem details response.
fn problem(kind: &str, detail: &str) -> Response<ResponseBody> {
    let (status, body) = api::problem(kind, detail);
    let body = Bytes::from(serde_json::to_vec(&body).unwrap_or_default());
    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    reply(status, "application/problem+json", body)
}

/// An answer computed for this request, gzipped when it is large and the client accepts it.
fn answer(body: Bytes, media: &str, request: &Request<Incoming>) -> Response<ResponseBody> {
    let accept = text(request, header::ACCEPT_ENCODING);
    let mut response = if body.len() > COMPRESS_ABOVE && negotiate(accept, false) == Encoding::Gzip
    {
        let mut response = reply(StatusCode::OK, media, gzip(&body, 1));
        response
            .headers_mut()
            .insert(header::CONTENT_ENCODING, HeaderValue::from_static("gzip"));
        response
    } else {
        reply(StatusCode::OK, media, body)
    };
    response
        .headers_mut()
        .insert(header::VARY, HeaderValue::from_static("Accept-Encoding"));
    response
}

fn text(request: &Request<Incoming>, name: HeaderName) -> Option<&str> {
    request.headers().get(name).and_then(|v| v.to_str().ok())
}

/// A precompressed file, or 304 when the client has this version in this coding.
fn file(asset: &Asset, request: &Request<Incoming>) -> Response<ResponseBody> {
    let encoding = asset.available(negotiate(text(request, header::ACCEPT_ENCODING), true));
    let etag = asset.etag(encoding);
    let unchanged = not_modified(text(request, header::IF_NONE_MATCH), &etag);
    let mut response = if unchanged {
        let mut response = reply(StatusCode::NOT_MODIFIED, asset.content_type, Bytes::new());
        response.headers_mut().remove(header::CONTENT_TYPE);
        response
    } else {
        reply(
            StatusCode::OK,
            asset.content_type,
            asset.body(encoding).clone(),
        )
    };
    let headers = response.headers_mut();
    // The client keeps a copy but asks each time whether it is current.
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    headers.insert(header::VARY, HeaderValue::from_static("Accept-Encoding"));
    if let Ok(value) = HeaderValue::from_str(&etag) {
        headers.insert(header::ETAG, value);
    }
    if let (Some(name), false) = (encoding.name(), unchanged) {
        headers.insert(header::CONTENT_ENCODING, HeaderValue::from_static(name));
    }
    response
}

fn events(shared: &Arc<Shared>, head: bool) -> Response<ResponseBody> {
    let (sender, receiver) = mpsc::channel::<Bytes>(4);
    let mut response = Response::new(Either::Right(EventBody { receiver }));
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/event-stream"),
    );
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    if head {
        drop(sender);
        return response;
    }
    let Ok(permit) = shared.streams.clone().try_acquire_owned() else {
        let mut refused = problem(
            "too-many-streams",
            "too many event streams are open; close another page and try again",
        );
        // Closed, so the connection is not held either.
        refused
            .headers_mut()
            .insert(header::CONNECTION, HeaderValue::from_static("close"));
        return refused;
    };
    let mut versions = shared.versions.subscribe();
    let mut stopping = shared.stopping.subscribe();
    let state = shared.clone();
    tokio::spawn(async move {
        let _permit = permit;
        let first = format!("retry: 2000\n{}", graph_event(&state));
        versions.mark_unchanged();
        if sender.send(Bytes::from(first)).await.is_err() {
            return;
        }
        loop {
            let chunk = tokio::select! {
                changed = versions.changed() => match changed {
                    Ok(()) => graph_event(&state),
                    Err(_) => break,
                },
                () = tokio::time::sleep(HEARTBEAT) => ": keep-alive\n\n".to_owned(),
                _ = stopping.changed() => break,
                () = sender.closed() => break,
            };
            if sender.send(Bytes::from(chunk)).await.is_err() {
                break;
            }
        }
    });
    response
}

/// The event that says which graph the server now holds: its version and size. The viewer
/// reloads on any event, whatever its data.
fn graph_event(shared: &Shared) -> String {
    let snapshot = shared.current();
    let version = &snapshot.file.version;
    let data = serde_json::json!({
        "version": version,
        "nodes": snapshot.graph.nodes,
        "edges": snapshot.graph.edges,
    });
    sse_message(version, &data.to_string())
}

fn json_answer(body: Vec<u8>, request: &Request<Incoming>) -> Response<ResponseBody> {
    answer(Bytes::from(body), "application/json", request)
}

impl Shared {
    fn cached(&self, version: &str, key: &str) -> Option<Arc<Cached>> {
        let cache = self
            .cache
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if cache.version == version {
            cache.entries.get(key).cloned()
        } else {
            None
        }
    }

    fn store(&self, version: &str, key: String, entry: Arc<Cached>) {
        let mut cache = self
            .cache
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if cache.version != version || cache.entries.len() >= CACHE_ENTRIES {
            cache.version = version.to_owned();
            cache.entries.clear();
        }
        cache.entries.insert(key, entry);
    }
}

/// An operation's answer: the one kept for this graph version, or one call to the backend on a
/// blocking thread, which holds the GIL only while Python answers.
async fn python(
    shared: &Arc<Shared>,
    operation: &'static Operation,
    arguments: Vec<(&'static str, Argument)>,
    page: Option<(Page, Option<String>)>,
    graph: Served,
    request: &Request<Incoming>,
) -> Response<ResponseBody> {
    let cached = operation.cached;
    let version = graph.version.as_str();
    let Some(backend) = shared.backend.clone() else {
        return problem(
            "internal-error",
            "this server has nothing to answer the API",
        );
    };
    // A list is paged here, from the whole answer, so every page comes from one Python call.
    let call: Vec<(&'static str, Argument)> = arguments
        .into_iter()
        .filter(|(name, _)| operation.paging.is_none() || !matches!(*name, "limit" | "cursor"))
        .collect();
    let named: Vec<Value> = call
        .iter()
        .map(|(name, value)| serde_json::json!([name, value.to_json()]))
        .collect();
    let key = format!("{}{}", operation.id, Value::from(named));
    let kept = if cached {
        shared.cached(version, &key)
    } else {
        None
    };
    let entry = match kept {
        Some(entry) => entry,
        None => {
            let Ok(permit) = shared.answering.clone().try_acquire_owned() else {
                let mut busy = problem(
                    "busy",
                    "the server is answering other questions; ask again in a moment",
                );
                busy.headers_mut()
                    .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
                return busy;
            };
            let id = operation.id;
            let given = graph.clone();
            let answered = tokio::task::spawn_blocking(move || {
                let _permit = permit;
                backend.answer(&given, id, &call)
            })
            .await;
            match answered {
                Ok(Outcome::Answer { media, body }) => {
                    let parsed = operation
                        .paging
                        .and_then(|_| serde_json::from_slice::<Value>(&body).ok());
                    let entry = Arc::new(Cached {
                        version: graph.version.clone(),
                        media,
                        body: Bytes::from(body),
                        parsed,
                        gzipped: OnceLock::new(),
                    });
                    if cached {
                        shared.store(&entry.version, key, entry.clone());
                    }
                    entry
                }
                Ok(Outcome::Problem { kind, detail }) => return problem(&kind, &detail),
                Err(_) => return problem("internal-error", "the answer failed"),
            }
        }
    };
    match (operation.paging, page, &entry.parsed) {
        (Some(paging), Some((page, made_for)), Some(parsed)) => {
            // A cursor and the page it leads to name the graph the answer came from.
            if made_for.is_some_and(|made_for| made_for != entry.version) {
                return problem(
                    "stale-cursor",
                    "the graph changed since this cursor was made; start again without it",
                );
            }
            let body = serde_json::to_vec(&api::paged(parsed, &paging, page, &entry.version))
                .unwrap_or_default();
            json_answer(body, request)
        }
        _ => kept_answer(&entry, request),
    }
}

/// A whole answer as it is kept, gzipped once when it is large and the client accepts it.
fn kept_answer(entry: &Cached, request: &Request<Incoming>) -> Response<ResponseBody> {
    let accept = text(request, header::ACCEPT_ENCODING);
    if entry.body.len() > COMPRESS_ABOVE && negotiate(accept, false) == Encoding::Gzip {
        let zipped = entry.gzipped.get_or_init(|| gzip(&entry.body, 1)).clone();
        let mut response = reply(StatusCode::OK, &entry.media, zipped);
        let headers = response.headers_mut();
        headers.insert(header::CONTENT_ENCODING, HeaderValue::from_static("gzip"));
        headers.insert(header::VARY, HeaderValue::from_static("Accept-Encoding"));
        return response;
    }
    answer(entry.body.clone(), &entry.media, request)
}

async fn api_answer(
    shared: &Arc<Shared>,
    operation: &'static Operation,
    value: Option<&str>,
    pairs: &[(String, String)],
    request: &Request<Incoming>,
) -> Response<ResponseBody> {
    let decoded = match value.map(|v| percent_decode(v, false)) {
        Some(None) => {
            return problem(
                "invalid-parameter",
                "the path is not valid percent-encoded UTF-8",
            );
        }
        Some(Some(decoded)) => Some(decoded),
        None => None,
    };
    let arguments = match api::arguments(operation, decoded, pairs) {
        Ok(arguments) => arguments,
        Err(message) => return problem("invalid-parameter", &message),
    };
    let snapshot = shared.current();
    let graph = Served {
        version: snapshot.file.version.clone(),
        bytes: snapshot.graph.bytes.clone(),
    };
    let page = match operation
        .paging
        .map(|paging| api::page(&paging, &arguments))
    {
        None => None,
        Some(Ok(page)) => Some(page),
        Some(Err(message)) => return problem("invalid-parameter", &message),
    };
    drop(snapshot);
    python(shared, operation, arguments, page, graph, request).await
}

/// The response to one request.
async fn respond(shared: Arc<Shared>, request: Request<Incoming>) -> Response<ResponseBody> {
    let mut response = respond_unsecured(&shared, &request).await;
    // By route, not by media type: a 304 for the page has no Content-Type, and the browser
    // applies the 304's policy to the copy it shows.
    let page = route(request.uri().path()) == Route::Page
        && matches!(response.status(), StatusCode::OK | StatusCode::NOT_MODIFIED);
    secure(response.headers_mut(), page.then_some(&shared.policy));
    response
}

async fn respond_unsecured(
    shared: &Arc<Shared>,
    request: &Request<Incoming>,
) -> Response<ResponseBody> {
    let head = request.method() == Method::HEAD;
    if request.method() != Method::GET && !head {
        let mut response = problem("method-not-allowed", "only GET and HEAD are allowed");
        response
            .headers_mut()
            .insert(header::ALLOW, HeaderValue::from_static("GET, HEAD"));
        return response;
    }
    // Two Host headers could name two servers, and a proxy in front may read the other one.
    if request.headers().get_all(header::HOST).iter().count() > 1 {
        return problem("bad-request", "the request has more than one Host header");
    }
    // HTTP/1.1 requires Host; an absolute request target carries its own authority instead.
    let host = request
        .uri()
        .authority()
        .map(|a| a.as_str())
        .or_else(|| text(request, header::HOST));
    if !shared.hosts.allows(host) {
        return problem(
            "host-not-allowed",
            "the Host header does not name this server",
        );
    }
    let Some(pairs) = query_pairs(request.uri().query()) else {
        return problem(
            "invalid-parameter",
            "the query string is not valid percent-encoded UTF-8",
        );
    };
    let route = route(request.uri().path());
    // The page may be opened from a link anywhere; everything else is for the page itself and
    // for clients that are not browsers, and the API runs git and reads the project's files.
    if route != Route::Page && from_another_site(request, &shared.hosts) {
        return problem(
            "cross-site",
            "requests from other sites are refused; open the viewer from this server",
        );
    }
    let mut set_cookie = false;
    if let Some(token) = &shared.token {
        let page = route == Route::Page;
        let given = presented(request.headers(), &pairs, page);
        if !given
            .iter()
            .any(|t| same_token(t.as_bytes(), token.as_bytes()))
        {
            let mut response = problem(
                "unauthorised",
                "this server needs its token: Authorization: Bearer <token>",
            );
            response
                .headers_mut()
                .insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Bearer"));
            return response;
        }
        set_cookie = page && pairs.iter().any(|(k, _)| k == "token");
    }
    let snapshot = shared.current();
    match route {
        Route::Page => {
            let mut response = file(&shared.page, request);
            if set_cookie
                && let Some(token) = &shared.token
                && let Ok(cookie) = HeaderValue::from_str(&format!(
                    "{TOKEN_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"
                ))
            {
                response.headers_mut().insert(header::SET_COOKIE, cookie);
            }
            response
        }
        Route::Graph => file(&snapshot.file, request),
        Route::Explain => file(&snapshot.explain, request),
        Route::Events => events(shared, head),
        Route::OpenApi => {
            let mut response = reply(StatusCode::OK, "application/json", shared.openapi.clone());
            response
                .headers_mut()
                .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
            response
        }
        Route::Api(operation, value) => {
            drop(snapshot);
            api_answer(shared, operation, value, &pairs, request).await
        }
        Route::NotFound => problem(
            "not-found",
            "no such route; /api/v1/openapi.json lists them",
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routes_are_fixed() {
        assert_eq!(route("/"), Route::Page);
        assert_eq!(route("/graph.json"), Route::Graph);
        assert_eq!(route("/explain.json"), Route::Explain);
        assert_eq!(route("/events"), Route::Events);
        assert_eq!(route("/api/v1/openapi.json"), Route::OpenApi);
        let Route::Api(operation, value) = route("/api/v1/nodes/model.shop.orders%23amount") else {
            panic!("not an operation");
        };
        assert_eq!(
            (operation.id, value),
            ("getNode", Some("model.shop.orders%23amount"))
        );
        assert!(matches!(route("/api/v1/search"), Route::Api(o, None) if o.id == "search"));
        for path in [
            "/../graph.json",
            "/graph.json/",
            "/GRAPH.JSON",
            "/konstruct-out/graph.json",
            "/api",
            "/api/node/x",
            "/api/v1/nodes/",
            "",
        ] {
            assert_eq!(route(path), Route::NotFound, "{path}");
        }
    }

    #[test]
    fn tokens_compare_whole() {
        assert!(same_token(b"0123456789abcdef", b"0123456789abcdef"));
        assert!(!same_token(b"0123456789abcdeX", b"0123456789abcdef"));
        assert!(!same_token(b"0123456789abcde", b"0123456789abcdef"));
        assert!(!same_token(b"0123456789abcdef0", b"0123456789abcdef"));
        assert!(!same_token(b"", b"0123456789abcdef"));
        let mut headers = HeaderMap::new();
        headers.insert(
            header::AUTHORIZATION,
            HeaderValue::from_static("Bearer abc"),
        );
        headers.insert(
            header::COOKIE,
            HeaderValue::from_static("a=1; konstruct_token=def"),
        );
        let pairs = vec![("token".to_owned(), "ghi".to_owned())];
        assert_eq!(presented(&headers, &pairs, true), ["abc", "def", "ghi"]);
        assert_eq!(presented(&headers, &pairs, false), ["abc", "def"]);
    }

    fn parameter<'a>(pairs: &'a [(String, String)], name: &str) -> Option<&'a str> {
        pairs
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }

    #[test]
    fn queries_decode_plus_and_escapes() {
        let pairs =
            query_pairs(Some("q=lifetime+value&kind=column&kind=model&x&from=a%23b")).unwrap();
        assert_eq!(parameter(&pairs, "q"), Some("lifetime value"));
        assert_eq!(parameter(&pairs, "x"), Some(""));
        assert_eq!(parameter(&pairs, "from"), Some("a#b"));
        assert_eq!(pairs.iter().filter(|(k, _)| k == "kind").count(), 2);
        assert!(query_pairs(Some("q=%")).is_none());
        assert!(query_pairs(Some("q=%+1")).is_none());
        assert_eq!(query_pairs(None), Some(Vec::new()));
    }

    #[test]
    fn hosts_must_name_the_server() {
        let check = HostCheck::new("127.0.0.1", 8000);
        for allowed in [
            "127.0.0.1:8000",
            "localhost:8000",
            "LOCALHOST:8000",
            "[::1]:8000",
            "localhost.:8000",
            "10.0.0.2:8000",
        ] {
            assert!(check.allows(Some(allowed)), "{allowed}");
        }
        for refused in [
            None,
            Some(""),
            Some("evil.example:8000"),
            Some("127.0.0.1:8001"),
            Some("127.0.0.1"),
            Some("localhost.evil.example:8000"),
            Some("[::1"),
            Some("[nothing]:8000"),
            Some("[::1]8000"),
            Some("127.0.0.1:port"),
        ] {
            assert!(!check.allows(refused), "{refused:?}");
        }
        let named = HostCheck::new("Box.LAN", 80);
        assert!(named.allows(Some("box.lan")));
        assert!(named.allows(Some("box.lan:80")));
        assert!(!named.allows(Some("other.lan")));
        assert!(HostCheck::new("[::1]", 9).allows(Some("[::1]:9")));
    }

    #[test]
    fn etags_compare_weakly_for_if_none_match() {
        let tag = "\"00ff\"";
        assert!(not_modified(Some("\"00ff\""), tag));
        assert!(not_modified(Some("\"a\", W/\"00ff\""), tag));
        assert!(not_modified(Some("*"), tag));
        assert!(!not_modified(Some("\"00fe\""), tag));
        assert!(!not_modified(None, tag));
        let asset = Asset::new(Bytes::from_static(b"{\"a\": 1}"), "application/json");
        assert_ne!(asset.etag(Encoding::Identity), asset.etag(Encoding::Gzip));
        assert_ne!(asset.etag(Encoding::Gzip), asset.etag(Encoding::Brotli));
        assert!(asset.etag(Encoding::Brotli).ends_with("-br\""));
    }

    #[test]
    fn encodings_follow_accept_encoding() {
        assert_eq!(negotiate(None, true), Encoding::Identity);
        assert_eq!(
            negotiate(Some("gzip, deflate, br, zstd"), true),
            Encoding::Brotli
        );
        assert_eq!(negotiate(Some("gzip, deflate, br"), false), Encoding::Gzip);
        assert_eq!(negotiate(Some("br;q=0.5, gzip"), true), Encoding::Gzip);
        assert_eq!(
            negotiate(Some("br;q=0, gzip;q=0"), true),
            Encoding::Identity
        );
        assert_eq!(negotiate(Some("*"), true), Encoding::Brotli);
        assert_eq!(negotiate(Some("*;q=0.2, br;q=0"), true), Encoding::Gzip);
        assert_eq!(negotiate(Some("identity"), true), Encoding::Identity);
        assert_eq!(negotiate(Some("GZIP;Q=0.9"), true), Encoding::Gzip);
        assert_eq!(negotiate(Some("x-gzip"), true), Encoding::Gzip);
    }

    #[test]
    fn compressed_files_decode_to_the_original() {
        let original: Vec<u8> = (0..20_000)
            .flat_map(|i: u32| format!("{{\"n\": {i}}},").into_bytes())
            .collect();
        let asset = Asset::new(Bytes::from(original.clone()), "application/json");
        assert_eq!(asset.body(Encoding::Identity), &original[..]);
        while asset.encoded.get().is_none() {
            std::thread::sleep(Duration::from_millis(1));
        }
        assert_eq!(asset.available(Encoding::Brotli), Encoding::Brotli);
        let mut unzipped = Vec::new();
        io::Read::read_to_end(
            &mut flate2::read::GzDecoder::new(&asset.body(Encoding::Gzip)[..]),
            &mut unzipped,
        )
        .unwrap();
        assert_eq!(unzipped, original);
        let mut unbrotlied = Vec::new();
        let brotlied = asset.body(Encoding::Brotli);
        brotli::BrotliDecompress(&mut &brotlied[..], &mut unbrotlied).unwrap();
        assert_eq!(unbrotlied, original);
        assert!(brotlied.len() < original.len() / 5);
    }

    #[test]
    fn events_are_unnamed_and_framed_line_by_line() {
        // The viewer listens with onmessage, which only unnamed events reach.
        assert_eq!(sse_message("1", "{\"a\":1}"), "id: 1\ndata: {\"a\":1}\n\n");
        assert_eq!(
            sse_message("2\r\nevent: x", "one\r\ntwo\n"),
            "id: 2event: x\ndata: one\ndata: two\ndata: \n\n"
        );
    }

    #[test]
    fn the_policy_names_the_page_scripts_and_styles_by_hash() {
        let page = "<html><head><style>a{}</style></head><body>\
                    <script type=\"application/json\" id=\"d\">{}</script>\
                    <script type=\"module\">go()</script></body></html>";
        let policy = content_security_policy(page);
        let digest = |text: &str| format!("'sha256-{}'", base64(&Sha256::digest(text.as_bytes())));
        assert!(
            policy.contains(&format!("script-src {};", digest("go()"))),
            "{policy}"
        );
        assert!(
            policy.contains(&format!("style-src {};", digest("a{}"))),
            "{policy}"
        );
        assert!(!policy.contains(&digest("{}")));
        assert!(policy.starts_with("default-src 'none';"));
        assert!(content_security_policy("<p>").contains("script-src 'none';"));
        assert_eq!(base64(b"abcd"), "YWJjZA==");
        assert_eq!(base64(b"abc"), "YWJj");
        assert_eq!(base64(b"ab"), "YWI=");
    }

    #[test]
    fn percent_decoding_rejects_bad_escapes() {
        assert_eq!(percent_decode("a%20b+c", true).as_deref(), Some("a b c"));
        assert_eq!(percent_decode("a+c", false).as_deref(), Some("a+c"));
        assert_eq!(percent_decode("%C3%A9", false).as_deref(), Some("\u{e9}"));
        assert!(percent_decode("%2", false).is_none());
        assert!(percent_decode("%-1", false).is_none());
    }
}
