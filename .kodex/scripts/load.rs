//! A load client for `konstruct serve`, with nothing but the standard library: CONCURRENCY
//! threads, each on one keep-alive connection, send GET requests for SECONDS, cycling through the
//! paths given, and the latency of every request is recorded.
//!
//! Built and run by benchmark_serve.py:
//!     rustc -O --edition 2024 -o load load.rs
//!     ./load 127.0.0.1:8000 16 5 /api/v1/search?q=c01 ...
//! Prints one JSON object: requests, errors, seconds, requests per second, p50 and p99 in ms.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

fn request(reader: &mut BufReader<TcpStream>, host: &str, path: &str) -> std::io::Result<u16> {
    let message = format!("GET {path} HTTP/1.1\r\nHost: {host}\r\nAccept-Encoding: identity\r\n\r\n");
    reader.get_mut().write_all(message.as_bytes())?;
    let mut line = String::new();
    reader.read_line(&mut line)?;
    let status: u16 = line.split(' ').nth(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut length = 0usize;
    loop {
        line.clear();
        if reader.read_line(&mut line)? == 0 {
            return Err(std::io::ErrorKind::UnexpectedEof.into());
        }
        let header = line.trim_end();
        if header.is_empty() {
            break;
        }
        if let Some((name, value)) = header.split_once(':')
            && name.eq_ignore_ascii_case("content-length")
        {
            length = value.trim().parse().unwrap_or(0);
        }
    }
    let mut body = vec![0u8; length];
    reader.read_exact(&mut body)?;
    Ok(status)
}

fn percentile(sorted: &[f64], share: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let index = ((sorted.len() as f64 - 1.0) * share).round() as usize;
    sorted[index]
}

fn main() {
    let arguments: Vec<String> = std::env::args().collect();
    if arguments.len() < 5 {
        eprintln!("usage: load ADDRESS CONCURRENCY SECONDS PATH...");
        std::process::exit(2);
    }
    let address = arguments[1].clone();
    let concurrency: usize = arguments[2].parse().expect("CONCURRENCY is a number");
    let seconds: f64 = arguments[3].parse().expect("SECONDS is a number");
    let paths: Vec<String> = arguments[4..].to_vec();
    let started = Instant::now();
    let deadline = started + Duration::from_secs_f64(seconds);
    let workers: Vec<_> = (0..concurrency)
        .map(|worker| {
            let (address, paths) = (address.clone(), paths.clone());
            std::thread::spawn(move || {
                let mut latencies = Vec::new();
                let mut errors = 0usize;
                let mut reader = BufReader::new(TcpStream::connect(&address).expect("connect"));
                reader.get_ref().set_nodelay(true).ok();
                let mut next = worker;
                while Instant::now() < deadline {
                    let path = &paths[next % paths.len()];
                    next += 1;
                    let sent = Instant::now();
                    match request(&mut reader, &address, path) {
                        Ok(200) => latencies.push(sent.elapsed().as_secs_f64() * 1000.0),
                        Ok(_) => errors += 1,
                        Err(_) => {
                            errors += 1;
                            reader = BufReader::new(TcpStream::connect(&address).expect("connect"));
                        }
                    }
                }
                (latencies, errors)
            })
        })
        .collect();
    let mut latencies = Vec::new();
    let mut errors = 0;
    for worker in workers {
        let (mine, failed) = worker.join().expect("a worker failed");
        latencies.extend(mine);
        errors += failed;
    }
    let elapsed = started.elapsed().as_secs_f64();
    latencies.sort_by(f64::total_cmp);
    println!(
        "{{\"requests\": {}, \"errors\": {}, \"seconds\": {:.3}, \"rps\": {:.0}, \"p50_ms\": {:.3}, \"p99_ms\": {:.3}}}",
        latencies.len(),
        errors,
        elapsed,
        latencies.len() as f64 / elapsed,
        percentile(&latencies, 0.5),
        percentile(&latencies, 0.99)
    );
}
