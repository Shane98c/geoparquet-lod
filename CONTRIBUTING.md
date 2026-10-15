# Contributing

This is an experimental implementation and draft format. COGP, Esri's overview
work, and this project are evolving approaches; this repository is an invitation
to compare designs, not a claim of an agreed overview standard. Compatibility
breaks are possible while the layout is being evaluated.

## Development

Use Python 3.12+, Node 22+, and uv. From the repository root:

```sh
uv sync --locked --group dev
npm --prefix js ci
npm --prefix js run build
uv run ruff check python tests examples benchmarks
uv run pytest -q
npm --prefix js test
npm --prefix js run demo:bundle
```

Build JavaScript before running pytest: Python integration tests invoke the JS
reader against generated fixtures. The standalone JS spill test skips without
a fixture; pytest supplies one and checks it. Tests generate their own small
data and do not need the Japan archives. DuckDB may download its spatial
extension on first use. CI runs both language suites and bundles the demo.

For a build smoke test without an external dataset:

```sh
uv run python examples/quickstart.py /tmp/lod-example
uv run geoparquet-lod validate /tmp/lod-example/buildings.lod.parquet
```

This creates 5,000 synthetic rectangles to check the build pipeline, not to
demonstrate realistic map performance. Choose a fresh output directory; the
example does not overwrite existing files.
For a local comparison with your own archives:

```sh
npm --prefix js run demo:compare -- --lod /path/indexed.parquet --pmtiles /path/baseline.pmtiles
```

Optional `--cogp` and `--esri` paths enable the other readers. See
[the reader guide](docs/usage.md) and [hosting notes](docs/hosted-comparison.md).
For a scripted four-reader measurement, see
[the comparison page](docs/comparison.md#reproducing).

## Changes and reports

- Describe the dataset, intended behavior, and what actually happened. For
  bugs, prefer a small synthetic fixture over a large private dataset.
- For format changes, update `spec/` alongside writer/reader behavior and
  include a round-trip or interoperability check. Preserve source geometry,
  attributes, and analytical access. Rewriting row order can change map row IDs.
- For performance reports, give the commit, archive construction options,
  reader versions, tile coordinates or navigation route, style properties,
  device/browser, network conditions, and cold/warm cache definitions. Report
  feature counts and visual differences with bytes, requests, and timing.
- Distinguish unchanged upstream releases from locally modified experiments.
  Avoid general speed rankings from a single dataset or unequal output.
- Keep generated datasets, credentials, caches, and deployment state out of
  commits. Commit small measurement results and reproducible scripts instead.
- Pull requests run a [reader performance gate](docs/performance-checks.md)
  that rejects increases in bytes, requests, or CPU time on a synthetic fixture.

Useful next work includes independent readers, conformance fixtures, other
geometry workloads, memory measurements, adaptive thinning, and opportunities
to bring the physical access design into other GeoParquet overview proposals.

## Repository map

| Path                     | Purpose                                              |
| ------------------------ | ---------------------------------------------------- |
| `python/geoparquet_lod/` | Writer, layout finalization, validator, CLI          |
| `js/src/`                | Range reader, tile rendering, MapLibre workers       |
| `spec/`                  | Logical payload contract and optional physical index |
| `tests/`, `js/test/`     | Correctness tests and measurement harnesses          |
| `examples/`              | Small runnable construction examples                 |
| `benchmarks/`            | Dataset-specific builds and recorded results         |
| `docs/`                  | Usage, comparisons, evaluation, design alternatives  |
| `demo/`                  | Local and hosted comparison UI and deck.gl example   |

See [THIRD_PARTY.md](THIRD_PARTY.md) before modifying or redistributing vendored
code. Package publishing and hosted dataset replacement are separate from a
source-code change; neither happens automatically in CI.
