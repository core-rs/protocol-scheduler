#!/usr/bin/env python3
"""Compare frozen echo variants and separately count CPU or wakeup work."""

import argparse
import csv
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import platform
import random
import shlex
import statistics
import subprocess
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("echo_run", ROOT / "benchmarks/echo/run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)

EVENTS = {
    "cpu": ["cycles:u", "cycles:k", "instructions:u", "instructions:k", "context-switches", "cpu-migrations"],
    "work": ["syscalls:sys_enter_io_uring_enter", "syscalls:sys_enter_write", "syscalls:sys_enter_futex",
             "io_uring:io_uring_task_work_run", "io_uring:io_uring_local_work_run", "io_uring:io_uring_complete"],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", action="append", required=True, help="NAME=BACKEND=BINARY")
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", default=["4:1:64"])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--counters", choices=EVENTS)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    variants = {}
    for item in args.variant:
        name, backend, binary = item.split("=", 2)
        if name in variants:
            parser.error("duplicate variant name")
        variants[name] = (backend, Path(binary).resolve())
    cases = [tuple(map(int, case.split(":"))) for case in args.cases]
    settings = SimpleNamespace(server_cpus=[0, 1, 2, 3], client_cpus=[4, 5, 6, 7],
        client_binary=args.client.resolve(), client_workers=4, warmup=0 if args.counters else 1, seconds=3)
    report = {
        "platform": platform.platform(), "hostname": platform.node(),
        "rustc": subprocess.check_output(["rustc", "-Vv"], text=True),
        "settings": vars(settings) | {"client_binary": str(settings.client_binary)},
        "variants": {name: {"binary": str(binary), "backend": backend,
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
            for name, (backend, binary) in variants.items()},
        "client_sha256": hashlib.sha256(settings.client_binary.read_bytes()).hexdigest(),
        "cases": cases, "repeats": args.repeats, "seed": 20261025,
        "counter_mode": args.counters, "events": EVENTS.get(args.counters),
        "note": "Counter-instrumented rates are mechanism checks, not capacity measurements.",
        "samples": [],
    }
    identities = {}
    for name, variant in report["variants"].items():
        identity = (variant["backend"], variant["sha256"])
        if identity in identities:
            parser.error(f"{name} and {identities[identity]} use the same backend and executable hash")
        identities[identity] = name
    jobs = list(itertools.product(range(args.repeats), cases, variants))
    random.Random(report["seed"]).shuffle(jobs)
    with tempfile.TemporaryDirectory(prefix="core-scheduler-wait-affinity-") as directory:
        wrapper = Path(directory) / "server"
        for repeat, (workers, connections, size), variant in jobs:
            backend, binary = variants[variant]
            raw = args.output.with_name(f"{args.output.stem}-{variant}-w{workers}-c{connections}-b{size}-r{repeat}.csv")
            if args.counters:
                wrapper.write_text("#!/bin/sh\nexec " + shlex.join(["sudo", "-n", "perf", "stat", "-x,", "-e",
                    ",".join(EVENTS[args.counters]), "-o", str(raw), "--", str(binary)]) + ' "$@"\n')
                wrapper.chmod(0o700)
            sample = run.sample(wrapper if args.counters else binary, backend, workers, connections, size, settings)
            sample.update(repeat=repeat, variant=variant)
            if args.counters:
                counters = {row[2]: float(row[0]) for row in csv.reader(raw.read_text().splitlines())
                    if len(row) >= 3 and row[2] in EVENTS[args.counters]}
                if set(counters) != set(EVENTS[args.counters]):
                    raise RuntimeError(f"missing counters in {raw}")
                sample.update(counters=counters, per_request={event: value / sample["requests"]
                    for event, value in counters.items()})
            report["samples"].append(sample)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(variant, workers, connections, repeat, round(sample["requests_per_second"]), flush=True)
    for case in cases:
        medians = {name: statistics.median(s["requests_per_second"] for s in report["samples"]
            if s["variant"] == name and (s["workers"], s["connections"], s["bytes"]) == case) for name in variants}
        print(case, json.dumps(medians), flush=True)


if __name__ == "__main__":
    main()
