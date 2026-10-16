# Documentation

Start with the [project overview](../README.md), [usage guide](usage.md),
[logical specification](../spec/SPEC.md), and
[physical spatial index](../spec/SPATIAL-INDEX.md).

- [Comparison with other formats](comparison.md): this layout, COGP, PMTiles,
  and Esri SOP measured on the same hosted Japan archives, with what each
  format stores and how it reads a tile.
- [Japan evaluation](japan-evaluation.md): display density calibrated against
  PMTiles, transfer sizes, storage costs, and source-data integrity for
  53.7 million buildings.
- [Hosted comparison](hosted-comparison.md): the public demo, dataset
  provenance, counter definitions, and how to rebuild the page.
- [Design alternatives](design-alternatives.md): layouts and encodings measured
  in the same role and why the current ones were chosen.

Raw measurements sit in `benchmarks/`. Local paths inside them identify the
measured fixtures; they are not download locations. Full archives are
intentionally excluded from Git.
