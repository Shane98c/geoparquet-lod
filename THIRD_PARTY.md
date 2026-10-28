# Prior work and third-party material

The project's original code is covered by the root [MIT license](LICENSE).
That license does not replace dependency licenses or dataset terms.

## Design and dependencies

- [COGP](https://github.com/Kanahiro/cloud-optimized-geoparquet) and
  [GeoParquet PR 305](https://github.com/opengeospatial/geoparquet/pull/305):
  the quantized coordinate transform informed this layout.
  The JS reader uses `@cogp/reader`. `js/src/mvt.ts` is adapted from its
  MVT, clipping, and property code, and `js/src/geometry.ts`'s `GeometryBuilder`
  from its geometry builder; Kanahiro Iguchi's MIT notice is retained in
  those files and [COGP-LICENSE.txt](js/src/COGP-LICENSE.txt).
- [GeoParquet PR 299](https://github.com/opengeospatial/geoparquet/pull/299) and
  [Esri Spatially Optimized Parquet](https://github.com/Esri/spatially-optimized-parquet):
  per-resolution geometry columns and page-index-based spatial access.
- [Tippecanoe](https://github.com/felt/tippecanoe): inspiration for zoom selection,
  feature dropping, and tiny-polygon display. Our estimated-byte thinning is
  not an exact implementation of Tippecanoe's compressed-tile decisions.
- The implementation also depends on DuckDB, PyArrow, NumPy, Shapely, click,
  hyparquet, hyparquet-compressors, lineclip, and zstddec. Dependency manifests and lock
  files record the actual versions. MapLibre, PMTiles, deck.gl,
  @geoarrow/deck.gl-layers, and apache-arrow power the demo.

## Vendored Esri comparison reader

`js/vendor/esri-sop/` contains code from
[`Esri/spatially-optimized-parquet`](https://github.com/Esri/spatially-optimized-parquet/tree/10ff683874db8b2d99c0f03b98354b5ea5f8199c/viewer/src),
commit `10ff683874db8b2d99c0f03b98354b5ea5f8199c`, under Apache-2.0.
The [upstream license](js/vendor/esri-sop/LICENSE) is retained. The implementation
files are unchanged; `common/dataset/datasets.ts` reduces the demo dataset type
and is marked as modified, and `index.ts` is a local export adapter. This code serves only the optional
comparison pane, not the GeoParquet LOD reader.

## Example and benchmark data

`examples/quickstart.py` generates synthetic footprints under the project's
license. No real buildings or external dataset download are needed for it.

The Japan comparison derives from Overture building footprints distributed in
Esri's SOP example. The root software license does not license that data.
Consult the [Overture attribution documentation](https://docs.overturemaps.org/attribution/)
and the source records for dataset-specific attribution and terms. Benchmark
archive versions, sizes, checksums, and links are in
[the provenance record](docs/hosted-comparison.md). No large data files are included
in this source repository.
