# Reader performance regression checks

Run from the repository root after `npm ci --prefix js` and `uv sync --locked`:

```sh
bash scripts/check-reader-performance.sh HEAD
```

The argument is the baseline Git revision (use the PR base SHA when reviewing a
branch). The script builds that revision in a temporary directory and builds the
candidate from the current working files. It never switches the checkout. The
current harness imports both builds with the same installed dependencies, so the
baseline does not need to contain the harness. Temporary files are removed on exit.

The fixture is generated once using `examples/quickstart.py`: 5,000 seeded
synthetic polygons with carried and source-only properties. Both readers consume
exactly the same bytes from memory; no dataset downloads or remote range requests
are involved. The fixed workload uses the fixture’s Denver geography and known columns.
Dependency installation requires network access, and fixture generation may fetch
the DuckDB spatial extension on first use. Once dependencies and that extension
are cached, fixture generation and benchmarking run locally.

Opening, a nine-tile viewport, property projection and an exact-geometry tile are
measured together. Fetched bytes and range requests must not increase at all.
Repeated runs must produce deterministic I/O counts and SHA-256 tile checksums;
baseline and candidate output bytes must match before any performance result is
accepted. A change to intentional output encoding needs a separately reviewed
harness update, not relaxed correctness checking.

CPU measurement separately times repeated cached rendering and repeated fresh
reader opening, decoding and rendering from in-memory bytes. Calibration chooses one
shared iteration count that gives each revision at least 200 ms of work, warming
both implementations before sampling. Seven sequential paired samples alternate
baseline/candidate order. Each candidate CPU median must be at most
`baseline median * 1.25 + 15 ms`. The additive allowance and sufficiently long
workload avoid failures caused by timer granularity or tiny workloads. This is a
CPU/decoding/rendering gate, not a simulated network latency benchmark. JSON output includes
all samples, iterations, deterministic I/O counts and checksums for diagnosis.

The `reader performance` PR workflow uses the actual PR base SHA. Guard behavior
is also tested without timing variability:

```sh
node js/test/performance-gate.test.mjs
```

That self-test deliberately increases bytes, requests and CPU separately and
asserts each is rejected. This compact fixture covers reader opening, spatial
selection, source projection, exact geometry and warm rendering; it cannot replace
large-dataset memory profiles or browser/worker navigation benchmarks.
