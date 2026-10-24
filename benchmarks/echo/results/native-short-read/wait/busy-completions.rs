// Released under the MIT License.
// Copyright, 2026, by Samuel Williams.

#![cfg(target_os = "linux")]

use core_scheduler::{
    Cancellation, SocketRegistration, SocketStreamReader, SocketStreamReading, Spawn,
};
use core_scheduler_iouring::{IoUringOptions, IoUringSelector};
use core_scheduler_native::{NativeExecutorOptions, NativeScheduler};
use futures_lite::future::poll_once;
use std::{
    io::Write,
    net::{TcpListener, TcpStream},
    pin::pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::Duration,
};

struct StopTask(Arc<AtomicBool>);
impl Drop for StopTask {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}

#[test]
fn a_self_waking_task_does_not_starve_socket_completions() {
    let (selector, drivers) = IoUringSelector::with_workers(
        IoUringOptions {
            entries: 8,
            ..Default::default()
        },
        1,
    )
    .unwrap();
    let scheduler = Arc::new(
        NativeScheduler::with_drivers(
            selector,
            drivers,
            NativeExecutorOptions {
                workers: 1,
                ..Default::default()
            },
        )
        .unwrap(),
    );
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let mut peer = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
    let socket = scheduler
        .register_socket(listener.accept().unwrap().0)
        .unwrap();
    let stop = StopTask(Arc::new(AtomicBool::new(false)));
    let running = Arc::clone(&stop.0);
    let busy = scheduler
        .spawn(async move {
            while !running.load(Ordering::Acquire) {
                futures_lite::future::yield_now().await;
            }
        })
        .unwrap();
    let (started, waiting) = mpsc::channel();
    let (complete, completed) = mpsc::channel();
    let reading_scheduler = Arc::clone(&scheduler);
    let reading = scheduler
        .spawn(async move {
            let mut reader = reading_scheduler.socket_stream_reader(&socket).unwrap();
            let never = Cancellation::never();
            let mut byte = [0; 1];
            let mut operation = pin!(reader.read_into(&mut byte, &never));
            assert!(poll_once(&mut operation).await.is_none());
            started.send(()).unwrap();
            let count = operation.await.unwrap();
            complete.send(count).unwrap();
        })
        .unwrap();
    waiting.recv_timeout(Duration::from_secs(5)).unwrap();
    peer.write_all(b"x").unwrap();
    let observed = completed.recv_timeout(Duration::from_secs(1));
    stop.0.store(true, Ordering::Release);
    // Let a strict wait-only prototype finish cleanup before reporting its failed progress check.
    scheduler.block_on(async {
        busy.await.unwrap();
        reading.await.unwrap();
        scheduler.shutdown().await;
    });
    assert_eq!(observed.expect("busy task starved socket completion"), 1);
}
