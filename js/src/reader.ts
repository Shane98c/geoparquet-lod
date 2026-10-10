import {
  parquetMetadataAsync,
  parquetSchema,
  readColumnIndex,
  readOffsetIndex,
  type AsyncBuffer,
  type ColumnChunk,
  type FileMetaData,
  type SchemaTree,
} from 'hyparquet'
import { readColumn } from 'hyparquet/src/column.js'
import { DEFAULT_PARSERS } from 'hyparquet/src/convert.js'
import { getSchemaPath } from 'hyparquet/src/schema.js'
import { compressors } from 'hyparquet-compressors'
import type { AttributeSpec } from './payload.js'

export type Bbox = [number, number, number, number]

export const LOD_KEY = 'lod_columns'
export const LOD_VERSION = '0.1.0'

export interface Lod {
  name: string
  zooms: number[]
  geometry: 'point_f32' | 'polygon_varint' | 'line_varint'
  scale?: [number, number]
  offset?: [number, number]
}

export interface LodMetadata {
  version: string
  column: string
  order: string
  zooms: number[]
  tile_size: number
  prefix: { zoom: number; row_group_end: number } | null
  attributes: AttributeSpec[]
  thinning: Record<string, unknown>
  lods: Record<string, Omit<Lod, 'name'>>
}

/** A hyparquet-style byte source whose slices can be aborted. */
export interface RangeSource {
  byteLength: number
  slice(start: number, end?: number, signal?: AbortSignal): Promise<ArrayBuffer>
}

interface PageLocation {
  rowStart: number
  rowEnd: number
  byteStart: number
  byteEnd: number
}

export interface Payloads {
  /** Global row index of each value, so a feature can be addressed later. */
  rows: number[]
  values: Uint8Array[]
}

/** Below this, a column chunk is read whole rather than through its page index. */
const WHOLE_CHUNK_BYTES = 256 * 1024
const BBOX_LEAVES = ['bbox.xmin', 'bbox.ymin', 'bbox.xmax', 'bbox.ymax'] as const

/**
 * Reads the LOD columns layout: rows in one Hilbert order with a coarse prefix,
 * a covering bbox with page statistics, and one binary column per zoom band.
 * A tile read is: row groups by bbox statistics, pages by the bbox page index,
 * then only the overlapping pages of the one band column.
 */
export class LodReader {
  readonly metadata: FileMetaData
  readonly lod: LodMetadata
  readonly lods: Record<string, Lod>
  private readonly file: RangeSource
  private readonly rowGroupStarts: number[]
  private readonly cache = new Map<string, Promise<unknown>>()
  private readonly elements = new Map<string, SchemaTree['element']>()

  private constructor(file: RangeSource, metadata: FileMetaData) {
    this.file = file
    this.metadata = metadata
    const geo = JSON.parse(
      metadata.key_value_metadata?.find((kv) => kv.key === 'geo')?.value ?? '{}',
    )
    const lod = geo[LOD_KEY] as LodMetadata | undefined
    if (!lod) throw new Error(`file has no geo.${LOD_KEY} metadata`)
    if (String(lod.version).split('.')[0] !== LOD_VERSION.split('.')[0]) {
      throw new Error(`${LOD_KEY} version ${lod.version} is not compatible with ${LOD_VERSION}`)
    }
    for (const [name, child] of Object.entries(lod.lods)) {
      if (!['point_f32', 'polygon_varint', 'line_varint'].includes(child.geometry)) {
        throw new Error(`lod ${name} uses unknown geometry encoding ${child.geometry}`)
      }
    }
    this.lod = lod
    this.lods = Object.fromEntries(
      Object.entries(lod.lods).map(([name, child]) => [name, { ...child, name }]),
    )
    let acc = 0
    this.rowGroupStarts = metadata.row_groups.map((rg) => {
      const start = acc
      acc += Number(rg.num_rows)
      return start
    })
    const visit = (node: SchemaTree) => {
      if (node.children.length) node.children.forEach(visit)
      else this.elements.set(node.path.join('.'), node.element)
    }
    visit(parquetSchema(metadata))
  }

  static async open(file: RangeSource): Promise<LodReader> {
    const metadata = await parquetMetadataAsync(file as unknown as AsyncBuffer, {
      initialFetchSize: 8,
    })
    const reader = new LodReader(file, metadata)
    reader.warmPrefix()
    return reader
  }

  /** The LOD whose zooms contain `zoom`, clamped to the coarsest or finest LOD. */
  lodForZoom(zoom: number): Lod {
    const all = Object.values(this.lods)
    const exact = all.find((b) => b.zooms.includes(zoom))
    if (exact) return exact
    const zooms = all.flatMap((b) => b.zooms)
    const target = zoom > Math.max(...zooms) ? Math.max(...zooms) : Math.min(...zooms)
    return all.find((b) => b.zooms.includes(target))!
  }

  /** Non-null payloads of every row whose covering bbox may intersect `bbox`. */
  async readPayloads(
    lod: Lod,
    bbox: Bbox,
    signal?: AbortSignal,
    options: { refine?: boolean } = {},
  ): Promise<Payloads> {
    const out: Payloads = { rows: [], values: [] }
    const prefix = this.lod.prefix
    const coarse = prefix && Math.max(...lod.zooms) <= prefix.zoom
    const groups = this.metadata.row_groups
      .map((_, g) => g)
      .filter(
        (g) => (!coarse || g <= prefix!.row_group_end) && intersects(this.rowGroupBbox(g), bbox),
      )
    const leaf = `${this.lod.column}.${lod.name}`
    await Promise.all(
      groups.map(async (g) => {
        const start = this.rowGroupStarts[g]
        if (this.chunkBytes(g, leaf) <= WHOLE_CHUNK_BYTES) {
          for (const { row, value } of await this.readWholeLeaf(g, leaf)) {
            out.rows.push(start + row)
            out.values.push(value)
          }
          return
        }
        void this.offsetIndex(g, leaf).catch(() => undefined)
        let ranges = await this.candidateRows(g, bbox, signal)
        if (!ranges.length) return
        if (options.refine) ranges = await this.refineRows(g, ranges, bbox, signal)
        if (!ranges.length) return
        for (const { row, value } of await this.readLeafRows(g, leaf, ranges, signal)) {
          out.rows.push(start + row)
          out.values.push(value)
        }
      }),
    )
    return out
  }

  /** Values of flat columns for one global row, one page read per column. */
  async readRow(
    rowIndex: number,
    columns: string[],
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    const g = this.rowGroupStarts.findIndex(
      (start, i) =>
        rowIndex >= start &&
        (i + 1 === this.rowGroupStarts.length || rowIndex < this.rowGroupStarts[i + 1]),
    )
    if (g < 0) throw new Error(`row ${rowIndex} out of range`)
    const local = rowIndex - this.rowGroupStarts[g]
    const out: Record<string, unknown> = {}
    await Promise.all(
      columns.map(async (leaf) => {
        const values = await this.readLeafValues(g, leaf, [[local, local + 1]], signal)
        out[leaf] = values.get(local)
      }),
    )
    return out
  }

  private warmPrefix(): void {
    const prefix = this.lod.prefix
    if (!prefix) return
    for (let g = 0; g <= prefix.row_group_end; g++) {
      for (const leaf of BBOX_LEAVES) {
        void this.offsetIndex(g, leaf).catch(() => undefined)
        void this.columnIndex(g, leaf).catch(() => undefined)
      }
    }
  }

  private rowGroupBbox(g: number): Bbox {
    const stat = (leaf: string) => this.chunk(g, leaf).meta_data!.statistics!
    return [
      stat('bbox.xmin').min_value as unknown as number,
      stat('bbox.ymin').min_value as unknown as number,
      stat('bbox.xmax').max_value as unknown as number,
      stat('bbox.ymax').max_value as unknown as number,
    ]
  }

  private chunk(g: number, leaf: string): ColumnChunk {
    const chunk = this.metadata.row_groups[g].columns.find(
      (c) => c.meta_data?.path_in_schema.join('.') === leaf,
    )
    if (!chunk) throw new Error(`column ${leaf} missing in row group ${g}`)
    return chunk
  }

  private chunkBytes(g: number, leaf: string): number {
    return Number(this.chunk(g, leaf).meta_data!.total_compressed_size)
  }

  private async candidateRows(
    g: number,
    bbox: Bbox,
    signal?: AbortSignal,
  ): Promise<[number, number][]> {
    const rows = Number(this.metadata.row_groups[g].num_rows)
    const loaded = await Promise.all(
      BBOX_LEAVES.map((leaf) => Promise.all([this.offsetIndex(g, leaf), this.columnIndex(g, leaf)])),
    )
    signal?.throwIfAborted()
    let selected: [number, number][] | null = null
    for (const [i, [pages, index]] of loaded.entries()) {
      const ranges: [number, number][] = []
      index.min_values.forEach((minValue, p) => {
        const min = minValue as unknown as number
        const max = index.max_values[p] as unknown as number
        const tests = [min <= bbox[2], min <= bbox[3], max >= bbox[0], max >= bbox[1]]
        if (tests[i]) ranges.push([pages[p].rowStart, pages[p].rowEnd])
      })
      selected = selected ? intersectRanges(selected, ranges) : ranges
      if (!selected.length) return []
    }
    return mergeRanges(selected ?? [[0, rows]])
  }

  private async refineRows(
    g: number,
    ranges: [number, number][],
    bbox: Bbox,
    signal?: AbortSignal,
  ): Promise<[number, number][]> {
    const columns = await Promise.all(
      BBOX_LEAVES.map((leaf) => this.readLeafValues(g, leaf, ranges, signal)),
    )
    const hits: [number, number][] = []
    for (const [a, b] of ranges) {
      for (let row = a; row < b; row++) {
        const xmin = columns[0].get(row) as number
        const ymin = columns[1].get(row) as number
        const xmax = columns[2].get(row) as number
        const ymax = columns[3].get(row) as number
        if (xmin > bbox[2] || xmax < bbox[0] || ymin > bbox[3] || ymax < bbox[1]) continue
        const last = hits[hits.length - 1]
        if (last && last[1] === row) last[1] = row + 1
        else hits.push([row, row + 1])
      }
    }
    return hits
  }

  private decoder(g: number, leaf: string) {
    const chunk = this.chunk(g, leaf)
    return {
      ...chunk.meta_data!,
      pathInSchema: leaf.split('.'),
      element: this.elements.get(leaf)!,
      schemaPath: getSchemaPath(this.metadata.schema, leaf.split('.')),
      compressors,
      parsers: DEFAULT_PARSERS,
      utf8: false,
    }
  }

  private decodePages(
    buffer: ArrayBuffer,
    g: number,
    leaf: string,
    rowStart: number,
    rows: number,
  ): ArrayLike<unknown>[] {
    const { data } = readColumn(
      { view: new DataView(buffer), offset: 0 },
      { groupStart: rowStart, groupRows: rows, selectStart: 0, selectEnd: rows } as never,
      this.decoder(g, leaf) as never,
    )
    return data as ArrayLike<unknown>[]
  }

  private readWholeLeaf(g: number, leaf: string): Promise<{ row: number; value: Uint8Array }[]> {
    return this.cached(`w:${g}:${leaf}`, async () => {
      const chunk = this.chunk(g, leaf)
      const start = Number(chunk.meta_data!.dictionary_page_offset ?? chunk.meta_data!.data_page_offset)
      const buffer = await this.file.slice(start, start + this.chunkBytes(g, leaf))
      const rows = Number(this.metadata.row_groups[g].num_rows)
      const out: { row: number; value: Uint8Array }[] = []
      let row = 0
      for (const chunkData of this.decodePages(buffer, g, leaf, 0, rows)) {
        for (let i = 0; i < chunkData.length; i++, row++) {
          const v = unwrap(chunkData[i])
          if (v) out.push({ row, value: v })
        }
      }
      return out
    })
  }

  private async readLeafRows(
    g: number,
    leaf: string,
    ranges: [number, number][],
    signal?: AbortSignal,
  ): Promise<{ row: number; value: Uint8Array }[]> {
    const pages = await this.offsetIndex(g, leaf)
    const wanted = pages.filter((p) => ranges.some(([a, b]) => p.rowStart < b && p.rowEnd > a))
    if (!wanted.length) return []
    const runs: PageLocation[][] = []
    for (const p of wanted) {
      const run = runs[runs.length - 1]
      if (run && run[run.length - 1].byteEnd === p.byteStart) run.push(p)
      else runs.push([p])
    }
    const out: { row: number; value: Uint8Array }[] = []
    await Promise.all(
      runs.map(async (run) => {
        const buffer = await this.file.slice(run[0].byteStart, run[run.length - 1].byteEnd, signal)
        for (const page of run) {
          const slice = buffer.slice(page.byteStart - run[0].byteStart, page.byteEnd - run[0].byteStart)
          let row = page.rowStart
          for (const chunkData of this.decodePages(slice, g, leaf, page.rowStart, page.rowEnd - page.rowStart)) {
            for (let i = 0; i < chunkData.length; i++, row++) {
              if (!ranges.some(([a, b]) => row >= a && row < b)) continue
              const v = unwrap(chunkData[i])
              if (v !== undefined && v !== null) out.push({ row, value: v })
            }
          }
        }
      }),
    )
    return out
  }

  private async readLeafValues(
    g: number,
    leaf: string,
    ranges: [number, number][],
    signal?: AbortSignal,
  ): Promise<Map<number, unknown>> {
    const pages = await this.offsetIndex(g, leaf)
    const wanted = pages.filter((p) => ranges.some(([a, b]) => p.rowStart < b && p.rowEnd > a))
    const out = new Map<number, unknown>()
    await Promise.all(
      wanted.map(async (page) => {
        const buffer = await this.file.slice(page.byteStart, page.byteEnd, signal)
        let row = page.rowStart
        for (const chunkData of this.decodePages(buffer, g, leaf, page.rowStart, page.rowEnd - page.rowStart)) {
          for (let i = 0; i < chunkData.length; i++, row++) out.set(row, chunkData[i])
        }
      }),
    )
    return out
  }

  private offsetIndex(g: number, leaf: string): Promise<PageLocation[]> {
    return this.cached(`o:${g}:${leaf}`, async () => {
      const c = this.chunk(g, leaf)
      const buffer = await this.file.slice(
        Number(c.offset_index_offset),
        Number(c.offset_index_offset) + c.offset_index_length!,
      )
      const { page_locations } = readOffsetIndex({ view: new DataView(buffer), offset: 0 })
      const rows = Number(this.metadata.row_groups[g].num_rows)
      return page_locations.map((p, i) => ({
        rowStart: Number(p.first_row_index),
        rowEnd: i + 1 < page_locations.length ? Number(page_locations[i + 1].first_row_index) : rows,
        byteStart: Number(p.offset),
        byteEnd: Number(p.offset) + p.compressed_page_size,
      }))
    })
  }

  private columnIndex(g: number, leaf: string) {
    return this.cached(`c:${g}:${leaf}`, async () => {
      const c = this.chunk(g, leaf)
      const buffer = await this.file.slice(
        Number(c.column_index_offset),
        Number(c.column_index_offset) + c.column_index_length!,
      )
      return readColumnIndex({ view: new DataView(buffer), offset: 0 }, this.elements.get(leaf)!)
    })
  }

  private cached<T>(key: string, load: () => Promise<T>): Promise<T> {
    let p = this.cache.get(key) as Promise<T> | undefined
    if (!p) {
      p = load()
      this.cache.set(key, p)
      p.catch(() => this.cache.delete(key))
    }
    return p
  }
}

/** hyparquet wraps a struct child's value in a one-element array when the struct is nullable */
const unwrap = (v: unknown): Uint8Array | undefined =>
  Array.isArray(v) ? (v[0] as Uint8Array | undefined) : (v as Uint8Array | undefined)

const intersects = (a: Bbox, b: Bbox) => !(a[0] > b[2] || a[2] < b[0] || a[1] > b[3] || a[3] < b[1])

const mergeRanges = (ranges: [number, number][]): [number, number][] => {
  const sorted = [...ranges].sort((p, q) => p[0] - q[0])
  const out: [number, number][] = []
  for (const r of sorted) {
    const last = out[out.length - 1]
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1])
    else out.push([r[0], r[1]])
  }
  return out
}

const intersectRanges = (a: [number, number][], b: [number, number][]): [number, number][] => {
  const out: [number, number][] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    const s = Math.max(a[i][0], b[j][0])
    const e = Math.min(a[i][1], b[j][1])
    if (s < e) out.push([s, e])
    if (a[i][1] < b[j][1]) i++
    else j++
  }
  return out
}
