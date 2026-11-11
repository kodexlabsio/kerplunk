//! `konstruct serve` over real sockets: a server on a free port, a graph in a temporary
//! directory, and requests written by hand, so limits and framing are seen as a client sees them.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use _core::serve::api::Argument;
use _core::serve::{Backend, Options, Outcome, Served, Server, graph_version};

const PAGE: &str = "<!doctype html><html><head><style>p{}</style></head><body>\
                    <script type=\"module\">go()</script></body></html>";

fn graph(columns: &[&str]) -> String {
    let mut nodes =
        vec![r#"{"id": "model.a.orders", "kind": "model", "name": "orders"}"#.to_owned()];
    let mut edges = Vec::new();
    for column in columns {
        nodes.push(format!(
            r#"{{"id": "model.a.orders#{column}", "kind": "column", "name": "{column}", "parent": "model.a.orders"}}"#
        ));
        edges.push(format!(
            r#"{{"source": "model.a.orders", "target": "model.a.orders#{column}", "kind": "contains"}}"#
        ));
    }
    format!(
        "{{\"nodes\": [{}], \"edges\": [{}]}}",
        nodes.join(", "),
        edges.join(", ")
    )
}

fn directory(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!("konstruct-serve-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&path);
    std::fs::create_dir_all(&path).unwrap();
    path
}

/// Replace graph.json as a build does: a complete file renamed into place.
fn write_graph(directory: &std::path::Path, text: &str) {
    let temporary = directory.join(".graph.json.tmp");
    std::fs::write(&temporary, text).unwrap();
    std::fs::rename(&temporary, directory.join("graph.json")).unwrap();
}

/// Counts its calls, answers `explain` with them, and records the graph each call was given.
#[derive(Default)]
struct Counting {
    calls: AtomicUsize,
    given: std::sync::Mutex<Vec<(String, String)>>,
}

impl Counting {
    /// The version and text of the graph the last call was given.
    fn last(&self) -> (String, String) {
        self.given.lock().unwrap().last().cloned().unwrap()
    }
}

impl Backend for Counting {
    fn answer(
        &self,
        graph: &Served,
        operation: &str,
        arguments: &[(&'static str, Argument)],
    ) -> Outcome {
        let calls = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        let text = String::from_utf8(graph.bytes.to_vec()).unwrap();
        self.given
            .lock()
            .unwrap()
            .push((graph.version.clone(), text));
        match operation {
            "explain" => Outcome::Answer {
                media: "application/json".to_owned(),
                body: format!(
                    "{{\"calls\": {calls}, \"ref\": {:?}}}",
                    format!("{:?}", arguments[0].1)
                )
                .into_bytes(),
            },
            "listNodes" => Outcome::Answer {
                media: "application/json".to_owned(),
                body: br#"{"kinds": [], "items": [1, 2, 3]}"#.to_vec(),
            },
            _ => Outcome::Problem {
                kind: "not-answerable".to_owned(),
                detail: "no".to_owned(),
            },
        }
    }
}

struct Running {
    address: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
    directory: PathBuf,
}

impl Drop for Running {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
        let _ = std::fs::remove_dir_all(&self.directory);
    }
}

fn start(name: &str, configure: impl FnOnce(&mut Options)) -> Running {
    let directory = directory(name);
    write_graph(&directory, &graph(&["amount"]));
    let mut options = Options::new(&directory, "127.0.0.1", 0, PAGE.to_owned());
    options.poll = Duration::from_millis(50);
    configure(&mut options);
    let server = Server::bind(options).unwrap();
    let address = server.address();
    let stop = Arc::new(AtomicBool::new(false));
    let flag = stop.clone();
    let thread = std::thread::spawn(move || server.run(|| flag.load(Ordering::SeqCst)).unwrap());
    Running {
        address,
        stop,
        thread: Some(thread),
        directory,
    }
}

/// Send `request` and read until the server closes the connection or a whole response is in.
fn exchange(address: SocketAddr, request: &str) -> String {
    let mut stream = TcpStream::connect(address).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    stream.write_all(request.as_bytes()).unwrap();
    let mut received = Vec::new();
    let mut buffer = [0u8; 65536];
    loop {
        match stream.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(n) => received.extend_from_slice(&buffer[..n]),
        }
        let text = String::from_utf8_lossy(&received);
        if let Some((head, body)) = text.split_once("\r\n\r\n") {
            let length = head
                .lines()
                .find_map(|l| {
                    l.to_ascii_lowercase()
                        .strip_prefix("content-length: ")
                        .map(str::to_owned)
                })
                .and_then(|l| l.trim().parse::<usize>().ok());
            if length.is_some_and(|length| body.len() >= length) || head.contains(" 304 ") {
                break;
            }
        }
    }
    String::from_utf8_lossy(&received).into_owned()
}

fn get(address: SocketAddr, path: &str, extra: &str) -> String {
    let port = address.port();
    exchange(
        address,
        &format!(
            "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n{extra}\r\n"
        ),
    )
}

fn header<'a>(response: &'a str, name: &str) -> Option<&'a str> {
    let (head, _) = response.split_once("\r\n\r\n")?;
    head.lines().find_map(|line| {
        let (key, value) = line.split_once(':')?;
        key.eq_ignore_ascii_case(name).then(|| value.trim())
    })
}

#[test]
fn answers_files_with_etags_and_head_without_bodies() {
    let server = start("files", |_| {});
    let response = get(server.address, "/graph.json", "");
    assert!(response.starts_with("HTTP/1.1 200"), "{response}");
    assert!(response.ends_with(&graph(&["amount"])));
    let etag = header(&response, "etag").unwrap().to_owned();
    let again = get(
        server.address,
        "/graph.json",
        &format!("If-None-Match: {etag}\r\n"),
    );
    assert!(again.starts_with("HTTP/1.1 304"), "{again}");
    // The codings follow the file by moments; until then it is sent as it is.
    let deadline = Instant::now() + Duration::from_secs(5);
    let gzipped = loop {
        let response = get(server.address, "/graph.json", "Accept-Encoding: gzip\r\n");
        if header(&response, "content-encoding") == Some("gzip") || Instant::now() > deadline {
            break response;
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    assert_eq!(header(&gzipped, "content-encoding"), Some("gzip"));
    assert_ne!(header(&gzipped, "etag").unwrap(), etag);
    let port = server.address.port();
    let head = exchange(
        server.address,
        &format!(
            "HEAD /graph.json HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
        ),
    );
    assert!(head.starts_with("HTTP/1.1 200"));
    assert_eq!(head.split_once("\r\n\r\n").unwrap().1, "");
    let page = get(server.address, "/", "");
    let policy = header(&page, "content-security-policy").unwrap();
    assert!(policy.contains("script-src 'sha256-"), "{policy}");
    assert_eq!(header(&page, "x-content-type-options"), Some("nosniff"));
    assert!(header(&page, "access-control-allow-origin").is_none());
}

#[test]
fn the_page_keeps_its_policy_when_it_has_not_changed() {
    let server = start("policy", |_| {});
    let page = get(server.address, "/", "");
    let policy = header(&page, "content-security-policy").unwrap().to_owned();
    assert!(policy.contains("script-src 'sha256-"), "{policy}");
    let etag = header(&page, "etag").unwrap().to_owned();
    // A reload revalidates the page: the 304 carries no Content-Type, and the browser applies
    // this response's policy to the copy it keeps.
    let again = get(server.address, "/", &format!("If-None-Match: {etag}\r\n"));
    assert!(again.starts_with("HTTP/1.1 304"), "{again}");
    assert_eq!(
        header(&again, "content-security-policy"),
        Some(policy.as_str())
    );
    // Anything else keeps the strict policy, whatever its media type.
    let graph = get(server.address, "/graph.json", "");
    let strict = header(&graph, "content-security-policy").unwrap();
    assert_eq!(strict, "default-src 'none'; frame-ancestors 'none'");
}

#[test]
fn limits_headers_connections_and_slow_clients() {
    let server = start("limits", |options| {
        options.header_timeout = Duration::from_millis(300);
    });
    let big = format!("X-Big: {}\r\n", "a".repeat(20_000));
    assert!(get(server.address, "/graph.json", &big).starts_with("HTTP/1.1 431"));
    // A client that never finishes its headers is disconnected.
    let mut slow = TcpStream::connect(server.address).unwrap();
    slow.write_all(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        .unwrap();
    slow.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let started = Instant::now();
    let mut buffer = [0u8; 1024];
    let read = slow.read(&mut buffer).unwrap_or(0);
    assert!(started.elapsed() < Duration::from_secs(3));
    assert!(read == 0 || String::from_utf8_lossy(&buffer[..read]).starts_with("HTTP/1.1 408"));
}

#[test]
fn closes_connections_over_the_limit() {
    let server = start("cap", |options| options.max_connections = 2);
    let mut buffer = [0u8; 1024];
    // Two idle connections fill the server; a third is closed at once.
    let held: Vec<TcpStream> = (0..2)
        .map(|_| TcpStream::connect(server.address).unwrap())
        .collect();
    std::thread::sleep(Duration::from_millis(100));
    let mut third = TcpStream::connect(server.address).unwrap();
    third
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let _ = third.write_all(b"GET /graph.json HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n");
    assert_eq!(third.read(&mut buffer).unwrap_or(0), 0);
    drop(held);
}

/// Open `/events` and return the stream with what came back first.
fn open_events(address: SocketAddr) -> (TcpStream, String) {
    let port = address.port();
    let mut stream = TcpStream::connect(address).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    stream
        .write_all(format!("GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n").as_bytes())
        .unwrap();
    let mut buffer = [0u8; 4096];
    let n = stream.read(&mut buffer).unwrap_or(0);
    (stream, String::from_utf8_lossy(&buffer[..n]).into_owned())
}

#[test]
fn event_streams_have_a_smaller_cap_of_their_own() {
    let server = start("streams", |options| options.max_event_streams = 2);
    let (first, answer) = open_events(server.address);
    assert!(answer.starts_with("HTTP/1.1 200"), "{answer}");
    let (_second, answer) = open_events(server.address);
    assert!(answer.starts_with("HTTP/1.1 200"), "{answer}");
    let (_third, refused) = open_events(server.address);
    assert!(refused.starts_with("HTTP/1.1 503"), "{refused}");
    assert!(refused.contains("too-many-streams"), "{refused}");
    assert_eq!(header(&refused, "connection"), Some("close"));
    // The rest of the API still answers while the streams are full.
    assert!(get(server.address, "/graph.json", "").starts_with("HTTP/1.1 200"));
    drop(first);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let (_again, answer) = open_events(server.address);
        if answer.starts_with("HTTP/1.1 200") {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "a closed stream kept its place: {answer}"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn a_request_with_two_hosts_is_refused() {
    let server = start("hosts", |_| {});
    let port = server.address.port();
    let twice = exchange(
        server.address,
        &format!(
            "GET /graph.json HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nHost: evil.example\r\nConnection: close\r\n\r\n"
        ),
    );
    assert!(twice.starts_with("HTTP/1.1 400"), "{twice}");
    assert!(twice.contains("bad-request"), "{twice}");
}

#[test]
fn reloads_a_replaced_graph_and_tells_event_streams() {
    let server = start("reload", |_| {});
    let port = server.address.port();
    let mut events = TcpStream::connect(server.address).unwrap();
    events
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    events
        .write_all(format!("GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n").as_bytes())
        .unwrap();
    let mut received = String::new();
    let mut buffer = [0u8; 4096];
    while !received.contains("\n\n") || !received.contains("data: {") {
        let n = events.read(&mut buffer).unwrap();
        received.push_str(&String::from_utf8_lossy(&buffer[..n]));
    }
    assert!(received.contains("text/event-stream"));
    assert!(received.contains("retry: 2000"));
    // Unnamed, so the viewer's onmessage receives them.
    assert!(!received.contains("event:"), "{received}");
    let before = get(server.address, "/graph.json", "");
    write_graph(&server.directory, &graph(&["amount", "tax"]));
    let mut more = String::new();
    while more.matches("data: {").count() < 1 {
        let n = events.read(&mut buffer).unwrap();
        more.push_str(&String::from_utf8_lossy(&buffer[..n]));
    }
    assert!(more.contains("\"nodes\":3"), "{more}");
    let after = get(server.address, "/graph.json", "");
    assert_ne!(header(&before, "etag"), header(&after, "etag"));
    assert!(after.ends_with(&graph(&["amount", "tax"])));
    // A file that does not parse leaves the graph as it was.
    write_graph(&server.directory, "{not json");
    std::thread::sleep(Duration::from_millis(300));
    let kept = get(server.address, "/graph.json", "");
    assert_eq!(header(&kept, "etag"), header(&after, "etag"));
    assert!(kept.ends_with(&graph(&["amount", "tax"])));
}

#[test]
fn python_answers_are_cached_per_graph_version_and_lists_paged() {
    let backend = Arc::new(Counting::default());
    let shared: Arc<dyn Backend> = backend.clone();
    let server = start("backend", |options| options.backend = Some(shared));
    let first = get(server.address, "/api/v1/explain/orders.amount", "");
    let second = get(server.address, "/api/v1/explain/orders.amount", "");
    assert!(first.contains("\"calls\": 1"), "{first}");
    assert!(second.contains("\"calls\": 1"), "{second}");
    assert!(get(server.address, "/api/v1/explain/orders.other", "").contains("\"calls\": 2"));
    let page = get(server.address, "/api/v1/nodes?limit=2", "");
    assert!(page.contains("\"items\":[1,2]"), "{page}");
    let cursor = page
        .split("\"next_cursor\":\"")
        .nth(1)
        .unwrap()
        .split('"')
        .next()
        .unwrap()
        .to_owned();
    let rest = get(
        server.address,
        &format!("/api/v1/nodes?limit=2&cursor={cursor}"),
        "",
    );
    assert!(
        rest.contains("\"items\":[3]") && rest.contains("\"next_cursor\":null"),
        "{rest}"
    );
    let problem = get(server.address, "/api/v1/ontology", "");
    assert!(problem.starts_with("HTTP/1.1 422"));
    assert_eq!(
        header(&problem, "content-type"),
        Some("application/problem+json")
    );
    write_graph(&server.directory, &graph(&["amount", "tax"]));
    let deadline = Instant::now() + Duration::from_secs(5);
    while !get(server.address, "/api/v1/explain/orders.amount", "").contains("\"calls\": 5") {
        assert!(Instant::now() < deadline, "the cache outlived its graph");
        std::thread::sleep(Duration::from_millis(50));
    }
    let stale = get(
        server.address,
        &format!("/api/v1/nodes?cursor={cursor}"),
        "",
    );
    assert!(stale.starts_with("HTTP/1.1 409"), "{stale}");
}

#[test]
fn the_backend_answers_from_the_graph_the_server_serves() {
    let backend = Arc::new(Counting::default());
    let shared: Arc<dyn Backend> = backend.clone();
    let server = start("served", |options| options.backend = Some(shared));
    let wait_for = |text: &str| {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !get(server.address, "/graph.json", "").ends_with(text) {
            assert!(Instant::now() < deadline, "the server did not reload");
            std::thread::sleep(Duration::from_millis(50));
        }
    };
    let first = graph(&["amount"]);
    assert!(get(server.address, "/api/v1/explain/orders.amount", "").contains("\"calls\": 1"));
    assert_eq!(
        backend.last(),
        (graph_version(first.as_bytes()), first.clone())
    );
    // Files the server refuses are never given to the backend: it keeps answering from the
    // graph the server keeps serving, and what it answered about it stays kept.
    for refused in [
        "{not json".to_owned(),
        first.replacen('{', r#"{"format_version": 2, "#, 1),
        first.replacen('{', r#"{"format_version": "x", "#, 1),
    ] {
        write_graph(&server.directory, &refused);
        std::thread::sleep(Duration::from_millis(300));
        assert!(get(server.address, "/graph.json", "").ends_with(&first));
        assert!(get(server.address, "/api/v1/explain/orders.amount", "").contains("\"calls\": 1"));
        get(server.address, "/api/v1/explain/orders.tax", "");
        assert_eq!(
            backend.last(),
            (graph_version(first.as_bytes()), first.clone())
        );
    }
    // A good file again: it is served, and the backend is given it.
    let second = graph(&["amount", "tax"]);
    write_graph(&server.directory, &second);
    wait_for(&second);
    assert!(!get(server.address, "/api/v1/explain/orders.amount", "").contains("\"calls\": 1"));
    assert_eq!(backend.last(), (graph_version(second.as_bytes()), second));
}

/// Takes a while over every answer, as Python does over a large graph.
struct Slow;

impl Backend for Slow {
    fn answer(&self, _: &Served, _: &str, _: &[(&'static str, Argument)]) -> Outcome {
        std::thread::sleep(Duration::from_millis(400));
        Outcome::Answer {
            media: "application/json".to_owned(),
            body: b"{}".to_vec(),
        }
    }
}

#[test]
fn answers_queue_only_so_far_and_then_say_busy() {
    let server = start("busy", |options| {
        options.backend = Some(Arc::new(Slow));
        options.max_queued_answers = 2;
    });
    let address = server.address;
    let answers: Vec<String> = (0..6)
        .map(|n| std::thread::spawn(move || get(address, &format!("/api/v1/explain/c{n}"), "")))
        .collect::<Vec<_>>()
        .into_iter()
        .map(|thread| thread.join().unwrap())
        .collect();
    let busy: Vec<&String> = answers
        .iter()
        .filter(|a| a.starts_with("HTTP/1.1 503"))
        .collect();
    assert!(!busy.is_empty(), "{answers:?}");
    assert!(answers.iter().any(|a| a.starts_with("HTTP/1.1 200")));
    for answer in busy {
        assert!(answer.contains("#busy"), "{answer}");
        assert_eq!(header(answer, "retry-after"), Some("1"));
    }
    // Answered from the cache, a busy server still answers at once.
    assert!(get(address, "/graph.json", "").starts_with("HTTP/1.1 200"));
}

#[test]
fn requests_from_other_sites_are_refused() {
    let server = start("sites", |_| {});
    let port = server.address.port();
    for extra in [
        "Sec-Fetch-Site: cross-site\r\n".to_owned(),
        "Origin: http://evil.example\r\n".to_owned(),
        format!("Origin: http://evil.example:{port}\r\n"),
        "Origin: null\r\n".to_owned(),
        format!("Origin: https://127.0.0.1:{port}\r\n"),
    ] {
        for path in ["/graph.json", "/api/v1/status", "/events"] {
            let refused = get(server.address, path, &extra);
            assert!(
                refused.starts_with("HTTP/1.1 403"),
                "{path} {extra} {refused}"
            );
            assert!(refused.contains("#cross-site"), "{refused}");
        }
    }
    for extra in [
        "Sec-Fetch-Site: same-origin\r\n".to_owned(),
        "Sec-Fetch-Site: none\r\n".to_owned(),
        format!("Origin: http://127.0.0.1:{port}\r\n"),
        format!("Origin: http://localhost:{port}\r\n"),
    ] {
        let allowed = get(server.address, "/graph.json", &extra);
        assert!(allowed.starts_with("HTTP/1.1 200"), "{extra} {allowed}");
    }
    // A link from another site still opens the viewer.
    let page = get(server.address, "/", "Sec-Fetch-Site: cross-site\r\n");
    assert!(page.starts_with("HTTP/1.1 200"), "{page}");
}

#[test]
fn a_token_guards_every_route() {
    let token = "0123456789abcdef0123";
    let server = start("token", |options| options.token = Some(token.to_owned()));
    let refused = get(server.address, "/graph.json", "");
    assert!(refused.starts_with("HTTP/1.1 401"), "{refused}");
    assert_eq!(header(&refused, "www-authenticate"), Some("Bearer"));
    assert!(
        get(
            server.address,
            "/graph.json",
            "Authorization: Bearer wrong-token-0123\r\n"
        )
        .starts_with("HTTP/1.1 401")
    );
    assert!(
        get(
            server.address,
            "/graph.json",
            &format!("Authorization: Bearer {token}\r\n")
        )
        .starts_with("HTTP/1.1 200")
    );
    let page = get(server.address, &format!("/?token={token}"), "");
    let cookie = header(&page, "set-cookie").unwrap();
    assert!(cookie.contains("HttpOnly") && cookie.contains("SameSite=Strict"));
    let value = cookie.split(';').next().unwrap();
    assert!(get(server.address, "/events", &format!("Cookie: {value}\r\n")).contains("200 OK"));
    // The query string carries the token only for the page.
    assert!(
        get(server.address, &format!("/graph.json?token={token}"), "").starts_with("HTTP/1.1 401")
    );
    let short = Options {
        token: Some("short".to_owned()),
        ..Options::new(&server.directory, "127.0.0.1", 0, String::new())
    };
    assert!(Server::bind(short).is_err());
}

#[test]
fn refuses_a_port_in_use_and_a_missing_graph() {
    let server = start("port", |_| {});
    let taken = Options::new(
        &server.directory,
        "127.0.0.1",
        server.address.port(),
        String::new(),
    );
    let error = Server::bind(taken).unwrap_err();
    assert!(error.to_string().contains("is in use"), "{error}");
    let empty = directory("empty");
    let error = Server::bind(Options::new(&empty, "127.0.0.1", 0, String::new())).unwrap_err();
    assert!(
        error.to_string().contains("run `konstruct build` first"),
        "{error}"
    );
    let _ = std::fs::remove_dir_all(empty);
}
