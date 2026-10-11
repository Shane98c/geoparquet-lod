// The slice of the viewer's dataset model that MapLibreParquetLayer reads.
export interface PresetDataset {
  parquet: { url: string }
  byteSize: number
}
