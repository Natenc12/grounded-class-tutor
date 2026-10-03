"""Frozen CPU timing protocol; synthetic text only, networking prohibited in workers."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import resource
import selectors
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "assets.json"


def no_network(*args, **kwargs):
    raise RuntimeError("Network is prohibited in the local embedding benchmark")


def worker(args):
    key = args.worker
    encoder_path = args.encoder.resolve()
    started = time.perf_counter()
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network
    module = importlib.util.spec_from_file_location("study_encoder", encoder_path)
    encoder_module = importlib.util.module_from_spec(module)
    module.loader.exec_module(encoder_module)
    imports_ms = (time.perf_counter() - started) * 1000
    protocol = json.loads(args.protocol.read_text())
    spec = next(item for item in json.loads(MANIFEST.read_text()) if item["key"] == key)
    spec = {
        **spec,
        "model_path": str(args.assets_root.resolve() / spec["model_path"]),
        "tokenizer_path": str(args.assets_root.resolve() / spec["tokenizer_path"]),
    }
    encoder = encoder_module.Encoder(spec)
    tick = time.perf_counter()
    encoder.query(protocol["query_text"])
    first_ms = (time.perf_counter() - tick) * 1000
    initial = dict(
        phase="first_query",
        key=key,
        imports_ms=imports_ms,
        model_load_ms=encoder.load_ms,
        tokenizer_load_ms=encoder.tokenizer_load_ms,
        session_load_ms=encoder.session_load_ms,
        first_query_encode_ms=first_ms,
        internal_start_to_query_ms=(time.perf_counter() - started) * 1000,
    )
    print(json.dumps(initial), flush=True)
    query_times = []
    for _ in range(protocol["warm_query_repetitions_per_trial"]):
        tick = time.perf_counter()
        encoder.query(protocol["query_text"])
        query_times.append((time.perf_counter() - tick) * 1000)
    base = (
        "Active recall means retrieving information from memory. Spaced practice distributes "
        "learning across separate study sessions. A student explains an idea, checks the original "
        "source, and corrects mistakes. Course materials provide examples, definitions, and "
        "evidence for the explanation."
    )
    words = (base.split() * 20)[: protocol["document_text_words"] - 2]
    docs = [
        " ".join(words) + f" Passage {index}." for index in range(protocol["document_batch_count"])
    ]
    for _ in range(protocol["document_warmup_batches"]):
        encoder.encode(docs)
    batch_times = []
    for _ in range(protocol["warm_document_batches_per_trial"]):
        tick = time.perf_counter()
        encoder.encode(docs)
        batch_times.append((time.perf_counter() - tick) * 1000)
    result = dict(
        phase="complete",
        key=key,
        warm_query_ms=query_times,
        warm_document_batch_ms=batch_times,
        document_stats=encoder.last_stats,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        fingerprint=encoder.fingerprint(),
        encoder_sha256=hashlib.sha256(args.encoder.read_bytes()).hexdigest(),
        network_blocked=True,
        python=sys.version,
    )
    print(json.dumps(result), flush=True)


def run(args):
    if sys.platform != "darwin":
        raise ValueError("This frozen RSS protocol uses macOS ru_maxrss byte units")
    # Preflight outside the timed child: do not download or trust changed assets.
    for candidate in json.loads(MANIFEST.read_text()):
        for asset in candidate["files"]:
            data = (args.assets_root / asset["path"]).read_bytes()
            if len(data) != asset["bytes"] or hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise ValueError(f"Pinned asset verification failed: {asset['path']}")
    output = args.output_root.absolute()
    output.mkdir(parents=True, exist_ok=True)
    protocol = json.loads(args.protocol.read_text())
    trials = []
    for order in protocol["trial_order"]:
        for key in order:
            tick = time.perf_counter()
            environment = {
                "PATH": "/usr/bin:/bin",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "TOKENIZERS_PARALLELISM": "false",
                "OMP_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2",
            }
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(Path(__file__).resolve()),
                    "--worker",
                    key,
                    "--encoder",
                    str(args.encoder.resolve()),
                    "--assets-root",
                    str(args.assets_root.resolve()),
                    "--output-root",
                    str(output),
                    "--protocol",
                    str(args.protocol.resolve()),
                ],
                cwd=output,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    if not selector.select(timeout=120):
                        raise TimeoutError("Embedding worker did not produce its first result")
                first = process.stdout.readline()
                fresh_ms = (time.perf_counter() - tick) * 1000
                remaining, errors = process.communicate(timeout=120)
                if process.returncode:
                    raise RuntimeError(errors)
                initial = json.loads(first)
                final = json.loads(remaining)
                trials.append(
                    {**initial, **final, "fresh_process_to_query_ms": fresh_ms, "stderr": errors}
                )
                (output / "benchmark-raw.json").write_text(json.dumps(trials, indent=2) + "\n")
                print(
                    key,
                    "fresh_ms",
                    round(fresh_ms, 2),
                    "peak_mib",
                    round(final["peak_rss_bytes"] / 1024**2, 2),
                    flush=True,
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    summary = {
        "protocol_sha256": hashlib.sha256(args.protocol.read_bytes()).hexdigest(),
        "encoder_sha256": hashlib.sha256(args.encoder.read_bytes()).hexdigest(),
        "candidates": {},
    }
    for key in ("bge", "minilm"):
        subset = [t for t in trials if t["key"] == key]
        query = sorted(v for t in subset for v in t["warm_query_ms"])
        batches = [v for t in subset for v in t["warm_document_batch_ms"]]
        summary["candidates"][key] = {
            "cold_trials": len(subset),
            "warm_query_samples": len(query),
            "warm_batch_samples": len(batches),
            "fresh_process_to_query_ms": [t["fresh_process_to_query_ms"] for t in subset],
            "fresh_process_to_query_median_ms": statistics.median(
                t["fresh_process_to_query_ms"] for t in subset
            ),
            "model_load_median_ms": statistics.median(t["model_load_ms"] for t in subset),
            "first_query_median_ms": statistics.median(t["first_query_encode_ms"] for t in subset),
            "warm_query_median_ms": statistics.median(query),
            "warm_query_p95_ms": query[int(len(query) * 0.95) - 1],
            "warm_16_passage_batch_median_ms": statistics.median(batches),
            "peak_rss_mib": [t["peak_rss_bytes"] / 1024**2 for t in subset],
            "document_stats": subset[-1]["document_stats"],
        }
    (output / "benchmark-summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["bge", "minilm"])
    parser.add_argument("--encoder", type=Path, required=True)
    parser.add_argument("--assets-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=HERE / "protocol.json")
    args = parser.parse_args()
    worker(args) if args.worker else run(args)
