# Documentation and experiment index

Start with the [project overview](../README.md), [usage guide](usage.md),
[logical specification](../spec/SPEC.md), and
[physical spatial index](../spec/SPATIAL-INDEX.md).

## Current comparisons

- [Hosted comparison](r2-comparison.md): immutable Japan dataset `6c3a4f9`,
  released COGP writer 2.2.2 / JS reader 2.1.4, Esri reference reader provenance,
  data links, and counter/cache definitions.
- [Density experiment](tippecanoe-dropping.md): latest local construction,
  closer PMTiles feature counts at z8/11/12, unchanged analytical rows. It is
  not yet the dataset on the hosted page. Reproduction uses
  `benchmarks/write-japan-tippecanoe.py` and `js/test/dropping-profile.mjs`.

## Design history

These reports describe the implementation at the time of each experiment.
Their results are not all measurements of the current reader or dataset.

| Report                                            | What it isolates                                                      |
| ------------------------------------------------- | --------------------------------------------------------------------- |
| [Original benchmarks](benchmarks.md)              | Pre-index layout; older COGP 2.2.1 conversion                         |
| [Spatial index](parity-experiment.md)             | Bootstrap, directories, tile-aligned pages                            |
| [Single order and rendering](parity-followup.md)  | Removing the prefix; decoding and MVT work                            |
| [Workers and pages](parity-workers.md)            | CPU pool and 64 KiB pages                                             |
| [Separate attributes](parity-separate.md)         | Optional attribute-column alternative; packed remains preferred       |
| [Style changes](parity-styles.md)                 | Geometry reuse and source-attribute hydration                         |
| [Navigation](parity-navigation.md)                | Cancellation, caching, and abandoned work                             |
| [Modified COGP checkout](cogp-checkout-parity.md) | A separate patched-upstream feasibility experiment, not released COGP |

Raw JSON measurements sit beside the reports or in `benchmarks/`. Local paths
inside historical results identify the measured fixtures; they are not portable
download locations. Use the hosted provenance page or rebuild with your own
paths. Full archives are intentionally excluded from Git.
