import { SharedLoad } from './shared-load.js'
import {
  parquetMetadata,
  parquetMetadataAsync,
  parquetSchema,
  readColumnIndex,
  readOffsetIndex,
  type AsyncBuffer,
  type ColumnChunk,
  type FileMetaData,
  type SchemaTree,
} from 'hyparquet'
import { assembleNested } from 'hyparquet/src/assemble.js'
import { readColumn } from 'hyparquet/src/column.js'
import { DEFAULT_PARSERS } from 'hyparquet/src/convert.js'
import { isListLike, isMapLike, getSchemaPath } from 'hyparquet/src/schema.js'
import { compressors } from './compressors.js'
import { attributeKind, decodeRow, type AttributeField } from './decode.js'
import { decodeProjection, type ProjectionExecutor, type ProjectionTask } from './projection.js'
import type { GeometryType } from './geometry.js'
import { tileInterval, type Interval } from './key.js'

export type Bbox = [number, number, number, number]

export const LOD_KEY = 'lod_columns'
export const LOD_VERSION = '0.1.0'
export const ENCODING = 'packed'
export const PACK_VERSION = 1

/** Same major version, and the same minor while the major is 0. */
export function isCompatibleVersion(version: string): boolean {
  const [major, minor] = String(version).split('.')
  const [ourMajor, ourMinor] = LOD_VERSION.split('.')
  return major === ourMajor && (major !== '0' || minor === ourMinor)
}

export interface Lod {
  name: string
  zooms: number[]
  geometry_type: GeometryType
  scale: [number, number]
  offset: [number, number]
  /** Width in grid cells of the square drawn for a shape too small for the grid. */
  placeholder?: number
}

export interface LodMetadata {
  version: string
  column: string
  order: string
  zooms: number[]
  tile_size: number
  /** Stored Hilbert key column and its depth, when rows are ordered by it. */
  key?: { column: string; depth: number }
  encoding: string
  pack_version: number
  attributes: { name: string; type: string; values?: string[] }[]
  thinning: Record<string, unknown>
  lods: Record<string, Omit<Lod, 'name'>>
}

/** A hyparquet-style byte source whose slices can be aborted. */
export interface RangeSource {
  byteLength: number
  slice(start: number, end?: number, signal?: AbortSignal): Promise<ArrayBuffer>
}

export interface PageLocation {
  rowStart: number
  rowEnd: number
  byteStart: number
  byteEnd: number
}

/** Rows shown at one LOD inside a bbox: decoded geometry and attributes by name. */
export interface LodRows {
  /** Global row index of each value, so a feature can be addressed later. */
  rows: number[]
  coords: unknown[]
  columns: Record<string, unknown[]>
}

/** Rows for a tile finer than any LOD: exact WKB plus attributes from the finest LOD. */
export interface PrimaryRows {
  rows: number[]
  wkb: Uint8Array[]
  columns: Record<string, unknown[]>
}

/** Compressed bytes of decoded pages kept for later tiles, by kind; the least
 * recently used go first. Exact geometry pages are kept so tiles past the
 * finest LOD, at any zoom, reuse geometry an earlier tile fetched. */
const PAGE_CACHE_BYTES = { bbox: 4 * 1024 * 1024, geometry: 16 * 1024 * 1024 }
type PageKind = keyof typeof PAGE_CACHE_BYTES
/** LOD bytes a tile may read past its exact rows rather than fetch key pages to trim them. */
const LOOSE_BYTES = 32 * 1024
const BBOX_LEAVES = ['bbox.xmin', 'bbox.ymin', 'bbox.xmax', 'bbox.ymax'] as const

/**
 * Footer-based access to source columns, exact geometry, and unindexed LOD
 * files. The indexed map path uses SpatialReader and opens this reader lazily
 * for analytical queries.
 */
export class LodReader {
  readonly metadata: FileMetaData
  readonly lod: LodMetadata
  readonly lods: Record<string, Lod>
  /** Name of the primary (exact, WKB) geometry column. */
  readonly primary: string
  /** The finest zoom any LOD serves; beyond it a tile reads the primary geometry. */
  readonly finestZoom: number
  private readonly file: RangeSource
  private readonly rowGroupStarts: number[]
  private readonly cache = new Map<string, Promise<unknown>>()
  private readonly pages = new Map<
    string,
    { bytes: number; kind: PageKind; values: SharedLoad<Map<number, unknown>> }
  >()
  private readonly pageBytes: Record<PageKind, number> = { bbox: 0, geometry: 0 }
  // Cache requested scalar values, not whole decoded source pages. Sparse LOD
  // joins may scan millions of unselected rows. Account conservatively for keys
  // and Map entries; this is an estimated 32 MiB budget, not a heap guarantee.
  private readonly projected = new Map<string, { value: unknown; bytes: number }>()
  private projectedBytes = 0
  private readonly schema: SchemaTree
  private readonly elements = new Map<string, SchemaTree['element']>()
  private readonly footer: ArrayBuffer | undefined
  private digest: Promise<string> | undefined
  private readonly fields: AttributeField[]

  private constructor(file: RangeSource, metadata: FileMetaData, footer?: ArrayBuffer) {
    this.file = file
    this.metadata = metadata
    this.footer = footer
    const geo = JSON.parse(
      metadata.key_value_metadata?.find((kv) => kv.key === 'geo')?.value ?? '{}',
    )
    const lod = geo[LOD_KEY] as LodMetadata | undefined
    if (!lod) throw new Error(`file has no geo.${LOD_KEY} metadata`)
    if (!isCompatibleVersion(lod.version)) {
      throw new Error(`${LOD_KEY} version ${lod.version} is not compatible with ${LOD_VERSION}`)
    }
    if (lod.encoding !== ENCODING || lod.pack_version !== PACK_VERSION) {
      throw new Error(
        `${LOD_KEY} encoding ${lod.encoding}/${lod.pack_version} is not ${ENCODING}/${PACK_VERSION}`,
      )
    }
    for (const [name, child] of Object.entries(lod.lods)) {
      if (!['Point', 'MultiLineString', 'MultiPolygon'].includes(child.geometry_type)) {
        throw new Error(`lod ${name} has unknown geometry_type ${child.geometry_type}`)
      }
    }
    if (!lod.key) throw new Error(`${LOD_KEY} file stores no key column`)
    this.fields = lod.attributes.map((a) =>
      a.values
        ? { name: a.name, kind: 'enum' as const, values: a.values }
        : { name: a.name, kind: attributeKind(a.type) },
    )
    this.lod = lod
    this.lods = Object.fromEntries(
      Object.entries(lod.lods).map(([name, child]) => [name, { ...child, name }]),
    )
    this.primary = geo.primary_column ?? 'geometry'
    const column = geo.columns?.[this.primary]
    if (!isCrs84(column))
      throw new Error(`primary geometry ${this.primary} is not in OGC:CRS84 or EPSG:4326`)
    if ((column?.edges ?? 'planar') !== 'planar')
      throw new Error(`primary geometry ${this.primary} has ${column.edges} edges, not planar`)
    this.finestZoom = Math.max(...lod.zooms)
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
    this.schema = parquetSchema(metadata)
    visit(this.schema)
  }

  /** Top-level scalar columns of the file other than the covering, the LODs and the primary geometry. */
  scalarColumns(): string[] {
    const top = new Set<string>()
    for (const path of this.elements.keys()) {
      const [first, ...rest] = path.split('.')
      if (!rest.length && first !== this.primary) top.add(first)
    }
    return [...top]
  }

  /** Dataset extent from the ordinary covering statistics. */
  get bounds(): Bbox {
    const bounds: Bbox = [180, 90, -180, -90]
    for (let g = 0; g < this.metadata.row_groups.length; g++) {
      const box = this.rowGroupBbox(g)
      if (!box.every(Number.isFinite) || box[0] > box[2] || box[1] > box[3]) continue
      bounds[0] = Math.min(bounds[0], box[0])
      bounds[1] = Math.min(bounds[1], box[1])
      bounds[2] = Math.max(bounds[2], box[2])
      bounds[3] = Math.max(bounds[3], box[3])
    }
    return bounds[0] <= bounds[2] ? bounds : [-180, -85.051129, 180, 85.051129]
  }

  /** A reader over metadata found elsewhere than the footer, such as an
   * index's copy of some columns' metadata; it reads only those columns. */
  static fromMetadata(file: RangeSource, metadata: FileMetaData): LodReader {
    return new LodReader(file, metadata)
  }

  /** `footerBytes`, the footer's length when already known, reads the footer
   * and its trailer in one request. */
  static async open(
    file: RangeSource,
    signal?: AbortSignal,
    footerBytes?: number,
  ): Promise<LodReader> {
    if (footerBytes && footerBytes + 8 <= file.byteLength) {
      const tail = await file.slice(file.byteLength - footerBytes - 8, file.byteLength, signal)
      const view = new DataView(tail)
      if (view.getUint32(footerBytes, true) === footerBytes) {
        const metadata = parquetMetadata(tail)
        signal?.throwIfAborted()
        return new LodReader(file, metadata, tail.slice(0, footerBytes))
      }
    }
    let footer: ArrayBuffer | undefined
    const end = file.byteLength - 8
    const capturing: RangeSource = {
      byteLength: file.byteLength,
      async slice(start, stop, sliceSignal) {
        const buffer = await file.slice(start, stop, sliceSignal ?? signal)
        if (stop === end && start < end) footer = buffer
        return buffer
      },
    }
    // with an 8-byte initial fetch, the second slice is exactly the footer
    const metadata = await parquetMetadataAsync(capturing as unknown as AsyncBuffer, {
      initialFetchSize: 8,
    })
    signal?.throwIfAborted()
    return new LodReader(file, metadata, footer)
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

  /** Parquet leaf of an LOD: its packed rows. */
  lodLeaf(lod: Lod): string {
    return `${this.lod.column}.${lod.name}`
  }

  /**
   * Every row shown at `lod` in tile z/x/y, located through the stored
   * Hilbert key: row groups by the key's footer statistics, pages by its page
   * index, exact rows by the boundary key pages. Features centred outside the
   * tile that reach into it come as copies in the `spill` of a row inside the
   * interval or of the first row past it, so no bbox is read.
   */
  async readLodByTile(
    lod: Lod,
    z: number,
    x: number,
    y: number,
    attributes: string[] = [],
    signal?: AbortSignal,
  ): Promise<LodRows> {
    const key = this.lod.key
    if (!key) throw new Error('file has no stored key column')
    const out: LodRows = {
      rows: [],
      coords: [],
      columns: Object.fromEntries(attributes.map((a) => [a, []])),
    }
    const interval = tileInterval(key.depth, z, x, y)
    const [start, end] = interval
    const stats = (g: number) => {
      const s = this.chunk(g, key.column).meta_data!.statistics
      return s && s.min_value != null && s.max_value != null
        ? [unsigned(s.min_value), unsigned(s.max_value)]
        : [-Infinity, Infinity]
    }
    const all = this.metadata.row_groups.map((_, g) => g)
    const groups = all.filter((g) => {
      const [lo, hi] = stats(g)
      return lo < end && hi >= start
    })
    // the host of copies for this tile: the first row with key >= start
    const hostGroup = all.find((g) => stats(g)[1] >= start) ?? all[all.length - 1]
    if (hostGroup !== undefined && !groups.includes(hostGroup)) groups.push(hostGroup)
    const leaf = this.lodLeaf(lod)
    const tile = z * 2 ** 48 + x * 2 ** z + y
    const wanted = attributes.map((a) => this.fields.findIndex((f) => f.name === a))

    // groups adjacent in key order are spatial neighbours: warm their indexes so a pan
    // seldom meets a group cold
    for (const g of groups) {
      for (const n of [g - 1, g + 1]) {
        if (n < 0 || n >= all.length) continue
        void this.offsetIndex(n, key.column).catch(() => undefined)
        void this.columnIndex(n, key.column).catch(() => undefined)
        void this.offsetIndex(n, leaf).catch(() => undefined)
      }
    }
    const perGroup = await Promise.all(
      groups.map(async (g) => {
        const ranges = await this.keyRanges(
          g,
          key.column,
          [interval],
          g === hostGroup ? start : null,
          leaf,
          signal,
        )
        if (!ranges.length)
          return { g, values: new Map<number, unknown>(), keys: new Map<number, unknown>() }
        const [values, keys] = await Promise.all([
          this.readLeafRows(g, leaf, ranges, signal),
          this.readLeafValues(g, key.column, ranges, signal),
        ])
        return { g, values, keys }
      }),
    )
    for (const { g, values, keys } of perGroup) {
      const rowStart = this.rowGroupStarts[g]
      for (const [row, raw] of values) {
        const decoded = decodeRow(raw as Uint8Array, lod.geometry_type, this.fields)
        const own = decoded.geometry as unknown[]
        if (
          Number(keys.get(row)) >= start &&
          Number(keys.get(row)) < end &&
          (lod.geometry_type === 'Point' || own.length)
        ) {
          out.rows.push(rowStart + row)
          out.coords.push(decoded.geometry)
          attributes.forEach((a, i) => out.columns[a].push(decoded.attrs[wanted[i]]))
        }
        for (const copy of decoded.spill) {
          if (copy.tile !== tile) continue
          out.rows.push(copy.id)
          out.coords.push(copy.geometry)
          attributes.forEach((a, i) => out.columns[a].push(copy.attrs[wanted[i]]))
        }
      }
    }
    await this.hydrateColumns(out, attributes, signal)
    return out
  }

  /**
   * Row ranges of group `g` whose key lies in `intervals`: the key pages whose
   * statistics overlap, with the first and last page of each run read to find
   * the exact rows, since a page's rows run far past a fine tile's. With
   * `host`, also the first row whose key is at or past it.
   */
  private async keyRanges(
    g: number,
    column: string,
    intervals: Interval[],
    host: number | null,
    leaf: string,
    signal?: AbortSignal,
  ): Promise<[number, number][]> {
    const [pages, index, leafPages] = await Promise.all([
      this.offsetIndex(g, column),
      this.columnIndex(g, column),
      this.offsetIndex(g, leaf),
    ])
    signal?.throwIfAborted()
    const inside = (k: number) => intervals.some(([a, b]) => k >= a && k < b)
    const hit = pages.map((_, p) => {
      const lo = unsigned(index.min_values[p] as unknown as number)
      const hi = unsigned(index.max_values[p] as unknown as number)
      return intervals.some(([a, b]) => lo < b && hi >= a)
    })
    const hostPage =
      host === null
        ? -1
        : pages.findIndex((_, p) => unsigned(index.max_values[p] as unknown as number) >= host)
    if (hostPage >= 0) hit[hostPage] = true
    else if (host !== null && pages.length) hit[pages.length - 1] = true
    const runs: [number, number][] = []
    for (let p = 0; p < pages.length; p++) {
      if (!hit[p]) continue
      const last = runs[runs.length - 1]
      if (last && last[1] === p) last[1] = p + 1
      else runs.push([p, p + 1])
    }
    // Reading the key pages at a run's ends finds the exact rows; when the LOD pages those
    // rows could add weigh less than the key pages do, take the runs at page granularity.
    const leafBytes = (rows: [number, number][]) =>
      leafPages
        .filter((p) => rows.some(([a, b]) => p.rowStart < b && p.rowEnd > a))
        .reduce((n, p) => n + p.byteEnd - p.byteStart, 0)
    const whole = runs.map(([a, b]): [number, number] => [pages[a].rowStart, pages[b - 1].rowEnd])
    // an edge page's rows outside the interval are wasted reads; estimate them by the share of
    // the page's key span the interval misses, so an empty or tiny tile reads its key pages
    // rather than a run of LOD pages holding nothing of its own
    let waste = 0
    for (const [a, b] of runs) {
      for (const p of new Set([a, b - 1])) {
        const lo = unsigned(index.min_values[p] as unknown as number)
        const hi = unsigned(index.max_values[p] as unknown as number)
        const span = Math.max(hi - lo, 1)
        const covered = intervals.reduce(
          (n, [s0, e0]) => n + Math.max(0, Math.min(hi, e0 - 1) - Math.max(lo, s0)),
          0,
        )
        const miss = 1 - Math.min(1, covered / span)
        waste += leafBytes([[pages[p].rowStart, pages[p].rowEnd]]) * miss
      }
    }
    const keyBytes = runs.reduce((n, [a, b]) => {
      const edge = new Set([a, b - 1])
      return n + [...edge].reduce((m, p) => m + pages[p].byteEnd - pages[p].byteStart, 0)
    }, 0)
    if (waste <= Math.max(keyBytes, LOOSE_BYTES)) return mergeRanges(whole)
    const edges = new Set<number>()
    for (const [a, b] of runs) {
      edges.add(a)
      edges.add(b - 1)
    }
    if (hostPage >= 0) edges.add(hostPage)
    else if (host !== null && pages.length) edges.add(pages.length - 1)
    const edgePages = [...edges].sort((a, b) => a - b).map((p) => pages[p])
    const decoded = await this.pageValues(g, column, edgePages, signal)
    const exact = new Map<number, [number, number][]>()
    let hostRow = -1
    edgePages.forEach((page, i) => {
      const rows: [number, number][] = []
      let lastRow = -1
      for (const [row, value] of decoded[i]) {
        const k = unsigned(value as number)
        lastRow = row
        if (host !== null && hostRow < 0 && k >= host) hostRow = row
        if (!inside(k)) continue
        const last = rows[rows.length - 1]
        if (last && last[1] === row) last[1] = row + 1
        else rows.push([row, row + 1])
      }
      // past every key: the last row stands in as host
      if (host !== null && hostRow < 0 && i === edgePages.length - 1 && hostPage < 0)
        hostRow = lastRow
      exact.set(page.rowStart, rows)
    })
    const out: [number, number][] = []
    for (const [a, b] of runs) {
      for (let p = a; p < b; p++) {
        const page = pages[p]
        const rows = exact.get(page.rowStart)
        if (rows) out.push(...rows)
        else out.push([page.rowStart, page.rowEnd])
      }
    }
    if (hostRow >= 0) out.push([hostRow, hostRow + 1])
    return mergeRanges(out)
  }

  /**
   * Rows whose covering bbox intersects `bbox`, with their exact primary
   * geometry as WKB and the named attributes from the finest LOD, for tiles
   * finer than any LOD. Every row is shown at the finest LOD, so the bbox
   * pages alone choose the rows.
   */
  async readPrimary(
    bbox: Bbox,
    attributes: string[] = [],
    signal?: AbortSignal,
  ): Promise<PrimaryRows> {
    const out: PrimaryRows = {
      rows: [],
      wkb: [],
      columns: Object.fromEntries(attributes.map((a) => [a, []])),
    }
    const finest = this.lodForZoom(this.finestZoom)
    const wanted = attributes.map((a) => this.fields.findIndex((f) => f.name === a))

    const groups = this.metadata.row_groups
      .map((_, g) => g)
      .filter((g) => intersects(this.rowGroupBbox(g), bbox))
    const perGroup = await Promise.all(
      groups.map(async (g) => {
        let { ranges } = await this.candidateRows(g, bbox, signal)
        if (ranges.length) ranges = await this.refineRows(g, ranges, bbox, signal)
        if (!ranges.length)
          return {
            g,
            ranges,
            geometry: new Map<number, unknown>(),
            packed: new Map<number, unknown>(),
          }
        const [geometry, packed] = await Promise.all([
          this.readLeafValues(g, this.primary, ranges, signal),
          wanted.some((i) => i >= 0)
            ? this.readLeafRows(g, this.lodLeaf(finest), ranges, signal)
            : Promise.resolve(new Map<number, unknown>()),
        ])
        return { g, ranges, geometry, packed }
      }),
    )
    for (const { g, ranges, geometry, packed } of perGroup) {
      const start = this.rowGroupStarts[g]
      for (const [a, b] of ranges) {
        for (let row = a; row < b; row++) {
          const wkb = geometry.get(row)
          if (!(wkb instanceof Uint8Array)) continue
          out.rows.push(start + row)
          out.wkb.push(wkb)
          if (!attributes.length) continue
          const raw = packed.get(row)
          const attrs =
            raw instanceof Uint8Array ? decodeRow(raw, finest.geometry_type, this.fields).attrs : []
          attributes.forEach((name, i) => out.columns[name].push(attrs[wanted[i]] ?? null))
        }
      }
    }
    await this.hydrateColumns(out, attributes, signal)
    return out
  }

  /** Attributes that are not carried in the LOD payload come from the source columns. */
  private async hydrateColumns(
    out: { rows: number[]; columns: Record<string, unknown[]> },
    attributes: string[],
    signal?: AbortSignal,
  ): Promise<void> {
    await Promise.all(
      attributes.map(async (name) => {
        if (!this.fields.some((f) => f.name === name))
          out.columns[name] = (await this.readColumns(out.rows, [name], signal))[name]
      }),
    )
  }

  /** Batch projection of source leaves, preserving order and duplicate row IDs.
   * Reads each selected page once rather than issuing a lookup per feature. */
  async readColumns(
    rows: number[],
    columns: string[],
    signal?: AbortSignal,
    execute?: ProjectionExecutor,
  ): Promise<Record<string, unknown[]>> {
    signal?.throwIfAborted()
    const logical = new Map<string, { tree: SchemaTree; parts: string[] }>()
    for (const column of columns) {
      const exactTop = this.schema.children.find((node) => node.element.name === column)
      if (exactTop && !exactTop.children.length) continue
      if (!/^[^.[\]]+(?:\.[^.[\]]+|\[\d+\])*$/.test(column))
        throw new Error(`invalid column path ${column}`)
      const parts = column.replace(/\[(\d+)\]/g, '.$1').split('.')
      const top = this.schema.children.find((node) => node.element.name === parts[0])
      // An explicit physical LIST/MAP leaf path reads that leaf directly.
      if (
        this.elements.has(column) &&
        getSchemaPath(this.metadata.schema, column.split('.')).some(
          (node) => isListLike(node) || isMapLike(node),
        )
      )
        continue
      const prune = (tree: SchemaTree, depth: number): SchemaTree => {
        if (depth === parts.length - 1) return tree
        if (isListLike(tree)) {
          if (!/^\d+$/.test(parts[depth + 1]))
            throw new Error(`list path needs an index: ${column}`)
          let child = tree.children[0]
          if (child.children.length === 1) child = child.children[0]
          prune(child, depth + 1) // validate remaining path; preserve complete list shape
          return tree
        }
        if (isMapLike(tree)) {
          prune(tree.children[0].children[1], depth + 1) // map key then optional value path
          return tree
        }
        const child = tree.children.find((node) => node.element.name === parts[depth + 1])
        if (!child) throw new Error(`unknown column ${column}`)
        return { ...tree, children: [prune(child, depth + 1)] }
      }
      if (!top) throw new Error(`unknown column ${column}`)
      const tree = prune(top, 0)
      if (top.children.length) logical.set(column, { tree, parts })
    }

    const total = Number(this.metadata.num_rows)
    const groups = new Map<number, { local: number; output: number }[]>()
    rows.forEach((row, output) => {
      if (!Number.isSafeInteger(row) || row < 0 || row >= total)
        throw new Error(`row ${row} out of range`)
      let lo = 0,
        hi = this.rowGroupStarts.length
      while (lo + 1 < hi) {
        const mid = (lo + hi) >>> 1
        if (this.rowGroupStarts[mid] <= row) lo = mid
        else hi = mid
      }
      const entries = groups.get(lo) ?? []
      entries.push({ local: row - this.rowGroupStarts[lo], output })
      groups.set(lo, entries)
    })
    const out = Object.fromEntries(columns.map((name) => [name, new Array<unknown>(rows.length)]))
    await Promise.all(
      [...groups].map(async ([g, entries]) => {
        await Promise.all(
          columns.map(async (name) => {
            const key = (local: number) => `${name}:${local + this.rowGroupStarts[g]}`
            const missing = entries.filter((entry) => {
              const cached = this.projected.get(key(entry.local))
              if (!cached) return true
              out[name][entry.output] = cached.value
              this.projected.delete(key(entry.local))
              this.projected.set(key(entry.local), cached)
              return false
            })
            if (!missing.length) return
            const sorted = [...new Set(missing.map((e) => e.local))].sort((a, b) => a - b)
            const nested = logical.get(name)
            let projected: unknown[]
            if (nested) {
              const leaves: SchemaTree[] = []
              const visit = (tree: SchemaTree) => {
                if (tree.children.length) tree.children.forEach(visit)
                else leaves.push(tree)
              }
              visit(nested.tree)
              const data = new Map<string, unknown[]>()
              await Promise.all(
                leaves.map(async (leaf) => {
                  const path = leaf.path.join('.')
                  const repeated = getSchemaPath(this.metadata.schema, leaf.path).some(
                    (node) => node.element.repetition_type === 'REPEATED',
                  )
                  const task = await this.prepareProjection(g, path, sorted, signal, repeated)
                  data.set(path, execute ? await execute(task, signal) : decodeProjection(task))
                }),
              )
              assembleNested(data, nested.tree, RAW_GEOMETRY_PARSERS)
              projected = data.get(nested.parts[0])!.map((value) => {
                for (const part of nested.parts.slice(1)) {
                  if (value == null) return null
                  value = Object.hasOwn(value as object, part)
                    ? (value as Record<string, unknown>)[part]
                    : null
                }
                return normalizeNested(value)
              })
            } else {
              const task = await this.prepareProjection(g, name, sorted, signal)
              projected = execute ? await execute(task, signal) : decodeProjection(task)
            }
            signal?.throwIfAborted()
            const values = new Map(sorted.map((row, i) => [row, projected[i]]))
            for (const entry of missing) {
              const value = values.get(entry.local) ?? null
              out[name][entry.output] = value
              if (
                value !== null &&
                !['number', 'string', 'boolean', 'bigint'].includes(typeof value)
              )
                continue
              const k = key(entry.local)
              if (this.projected.has(k)) continue
              const bytes = 128 + k.length * 2 + (typeof value === 'string' ? value.length * 2 : 8)
              if (bytes > 32 * 1024 * 1024) continue
              while (this.projected.size && this.projectedBytes + bytes > 32 * 1024 * 1024) {
                const first = this.projected.keys().next().value!
                this.projectedBytes -= this.projected.get(first)!.bytes
                this.projected.delete(first)
              }
              this.projected.set(k, { value, bytes })
              this.projectedBytes += bytes
            }
          }),
        )
      }),
    )
    signal?.throwIfAborted()
    return out
  }

  private async prepareProjection(
    g: number,
    leaf: string,
    rows: number[],
    signal?: AbortSignal,
    wholeGroup = false,
  ): Promise<ProjectionTask> {
    const pages = await this.offsetIndex(g, leaf)
    const runs: PageLocation[][] = []
    let selected = 0
    for (const page of pages) {
      while (selected < rows.length && rows[selected] < page.rowStart) selected++
      if (!wholeGroup && (selected === rows.length || rows[selected] >= page.rowEnd)) continue
      const last = runs.at(-1)
      if (last && last.at(-1)!.byteEnd === page.byteStart && last.at(-1)!.rowEnd === page.rowStart)
        last.push(page)
      else runs.push([page])
    }
    const { type, codec, element, schemaPath, pathInSchema, utf8 } = this.decoder(g, leaf)
    const [dictionary, payloads] = await Promise.all([
      this.dictionaryPage(g, leaf),
      Promise.all(
        runs.map(async (run) => ({
          first: run[0].rowStart,
          count: run.at(-1)!.rowEnd - run[0].rowStart,
          bytes: await this.file.slice(run[0].byteStart, run.at(-1)!.byteEnd, signal),
        })),
      ),
    ])
    signal?.throwIfAborted()
    return {
      rows,
      decoder: { type, codec, element, schemaPath, pathInSchema, utf8 },
      dictionary,
      runs: payloads,
    }
  }

  /** Values of flat columns for one global row, one page read per column. */
  async readRow(
    rowIndex: number,
    columns: string[],
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    const values = await this.readColumns([rowIndex], columns, signal)
    return Object.fromEntries(columns.map((name) => [name, values[name][0]]))
  }

  /** Lowercase hex SHA-256 of the footer bytes read at open. */
  footerDigest(): Promise<string> {
    if (!this.digest) {
      const footer = this.footer
      if (!footer || footer.byteLength !== this.metadata.metadata_length)
        return Promise.reject(new Error('footer bytes from open do not match metadata_length'))
      this.digest = crypto.subtle
        .digest('SHA-256', footer)
        .then((hash) =>
          Array.from(new Uint8Array(hash), (b) => b.toString(16).padStart(2, '0')).join(''),
        )
    }
    return this.digest
  }

  /**
   * Every row of each page, as decoded. Pages adjacent in the file are
   * fetched as one range. Decoded bbox pages are kept, since neighbouring
   * tiles refine against the same ones; LOD pages decode too large to keep.
   */
  private async pageValues(
    g: number,
    leaf: string,
    pages: PageLocation[],
    signal?: AbortSignal,
  ): Promise<Map<number, unknown>[]> {
    signal?.throwIfAborted()
    const key = (page: PageLocation) => `p:${g}:${leaf}:${page.byteStart}`
    const kind: PageKind | null = (BBOX_LEAVES as readonly string[]).includes(leaf)
      ? 'bbox'
      : leaf === this.primary
        ? 'geometry'
        : null
    const ordered = [...pages].sort((a, b) => a.byteStart - b.byteStart)
    const wanted = new Map<string, Promise<Map<number, unknown>>>()
    const runs: PageLocation[][] = []
    for (const page of ordered) {
      const cached = this.pages.get(key(page))
      if (cached) this.pages.delete(key(page))
      if (cached && !cached.values.abandoned) {
        this.pages.set(key(page), cached)
        wanted.set(key(page), cached.values.get(signal))
        continue
      }
      if (cached) this.pageBytes[cached.kind] -= cached.bytes
      const last = runs[runs.length - 1]
      if (last && last[last.length - 1].byteEnd === page.byteStart) last.push(page)
      else runs.push([page])
    }
    const forget = (k: string) => {
      const entry = this.pages.get(k)
      if (!entry) return
      this.pages.delete(k)
      this.pageBytes[entry.kind] -= entry.bytes
    }
    const oldest = (kind: PageKind) => {
      for (const [k, entry] of this.pages) if (entry.kind === kind) return k
    }
    for (const run of runs) {
      const dictionary = this.dictionaryPage(g, leaf)
      // Page subscriptions keep the range alive until its last caller leaves.
      const buffer = new SharedLoad((signal) =>
        this.file.slice(run[0].byteStart, run[run.length - 1].byteEnd, signal),
      )
      for (const page of run) {
        const decoded = new SharedLoad((signal) =>
          Promise.all([buffer.get(signal), dictionary]).then(([bytes, dict]) => {
            const slice = bytes.slice(
              page.byteStart - run[0].byteStart,
              page.byteEnd - run[0].byteStart,
            )
            const values = new Map<number, unknown>()
            let row = page.rowStart
            for (const data of this.decodePages(slice, g, leaf, row, page.rowEnd - row, dict)) {
              for (let i = 0; i < data.length; i++, row++) values.set(row, data[i])
            }
            return values
          }),
        )
        wanted.set(key(page), decoded.get(signal))
        if (!kind) continue
        const bytes = page.byteEnd - page.byteStart
        if (bytes > PAGE_CACHE_BYTES[kind]) continue
        for (let k; this.pageBytes[kind] + bytes > PAGE_CACHE_BYTES[kind] && (k = oldest(kind));)
          forget(k)
        this.pages.set(key(page), { bytes, kind, values: decoded })
        this.pageBytes[kind] += bytes
        decoded.promise.catch(() => {
          if (this.pages.get(key(page))?.values === decoded) forget(key(page))
        })
      }
    }
    const values = await Promise.all(pages.map((page) => wanted.get(key(page))!))
    signal?.throwIfAborted()
    return values
  }

  private rowGroupBbox(g: number): Bbox {
    if (!Number(this.metadata.row_groups[g].num_rows)) return [1, 1, 0, 0]
    const stat = (leaf: string) => this.chunk(g, leaf).meta_data!.statistics
    if (!stat('bbox.xmin')) return [1, 1, 0, 0]
    return [
      stat('bbox.xmin')!.min_value as unknown as number,
      stat('bbox.ymin')!.min_value as unknown as number,
      stat('bbox.xmax')!.max_value as unknown as number,
      stat('bbox.ymax')!.max_value as unknown as number,
    ]
  }

  private chunk(g: number, leaf: string): ColumnChunk {
    const chunk = this.metadata.row_groups[g].columns.find(
      (c) => c.meta_data?.path_in_schema.join('.') === leaf,
    )
    if (!chunk) throw new Error(`column ${leaf} missing in row group ${g}`)
    return chunk
  }

  /** Row ranges admitted by all four bbox column indexes. */
  private async candidateRows(
    g: number,
    bbox: Bbox,
    signal?: AbortSignal,
  ): Promise<{ ranges: [number, number][] }> {
    const rows = Number(this.metadata.row_groups[g].num_rows)
    const loaded = await Promise.all(
      BBOX_LEAVES.map((leaf) =>
        Promise.all([this.offsetIndex(g, leaf), this.columnIndex(g, leaf)]),
      ),
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
      if (!selected.length) return { ranges: [] }
    }
    const ranges = mergeRanges(selected ?? [[0, rows]])
    return { ranges }
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
      pathInSchema: chunk.meta_data!.path_in_schema,
      element: this.elements.get(leaf)!,
      schemaPath: getSchemaPath(this.metadata.schema, chunk.meta_data!.path_in_schema),
      compressors,
      parsers: RAW_GEOMETRY_PARSERS,
      utf8: isString(this.elements.get(leaf)!),
    }
  }

  /** The dictionary page of a chunk, read once, so page runs can be decoded on their own. */
  private dictionaryPage(g: number, leaf: string): Promise<ArrayBuffer | null> {
    return this.cached(`d:${g}:${leaf}`, async () => {
      const meta = this.chunk(g, leaf).meta_data!
      if (meta.dictionary_page_offset === undefined) return null
      const start = Number(meta.dictionary_page_offset)
      const end = Number(meta.data_page_offset)
      return end > start ? this.file.slice(start, end) : null
    })
  }

  private decodePages(
    buffer: ArrayBuffer,
    g: number,
    leaf: string,
    rowStart: number,
    rows: number,
    dictionary: ArrayBuffer | null = null,
  ): ArrayLike<unknown>[] {
    if (dictionary) {
      const joined = new Uint8Array(dictionary.byteLength + buffer.byteLength)
      joined.set(new Uint8Array(dictionary), 0)
      joined.set(new Uint8Array(buffer), dictionary.byteLength)
      buffer = joined.buffer
    }
    const { data } = readColumn(
      { view: new DataView(buffer), offset: 0 },
      { groupStart: rowStart, groupRows: rows, selectStart: 0, selectEnd: rows } as never,
      this.decoder(g, leaf) as never,
    )
    return data as ArrayLike<unknown>[]
  }

  /** Rows of an LOD leaf within `ranges`, present rows only. */
  private async readLeafRows(
    g: number,
    leaf: string,
    ranges: [number, number][],
    signal?: AbortSignal,
  ): Promise<Map<number, unknown>> {
    const pages = await this.offsetIndex(g, leaf)
    const wanted = pages.filter((p) => ranges.some(([a, b]) => p.rowStart < b && p.rowEnd > a))
    const out = new Map<number, unknown>()
    for (const values of await this.pageValues(g, leaf, wanted, signal)) {
      for (const [row, raw] of values) {
        if (!ranges.some(([a, b]) => row >= a && row < b)) continue
        const v = unwrap(raw)
        if (v !== ABSENT) out.set(row, v)
      }
    }
    return out
  }

  /** Every row of the pages overlapping `ranges`, as decoded. */
  private async readLeafValues(
    g: number,
    leaf: string,
    ranges: [number, number][],
    signal?: AbortSignal,
  ): Promise<Map<number, unknown>> {
    const pages = await this.offsetIndex(g, leaf)
    const wanted = pages.filter((p) => ranges.some(([a, b]) => p.rowStart < b && p.rowEnd > a))
    const out = new Map<number, unknown>()
    for (const values of await this.pageValues(g, leaf, wanted, signal))
      for (const [row, value] of values) out.set(row, value)
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
        rowEnd:
          i + 1 < page_locations.length ? Number(page_locations[i + 1].first_row_index) : rows,
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

/** hyparquet would parse GEOMETRY-typed columns to GeoJSON; the tile encoder wants the WKB bytes. */
const RAW_GEOMETRY_PARSERS = {
  ...DEFAULT_PARSERS,
  geometryFromBytes: (bytes: Uint8Array) => bytes,
  geographyFromBytes: (bytes: Uint8Array) => bytes,
}

const isString = (element: SchemaTree['element']) =>
  element.converted_type === 'UTF8' ||
  (element.logical_type as { type?: string } | undefined)?.type === 'STRING'

const ABSENT = Symbol('absent')

/**
 * hyparquet assembles a leaf below the nullable LOD struct as one array per
 * row, empty when the struct is null, so a geometry leaf is `[value]` for a
 * shown row. A flat leaf (an attribute) comes back as the scalar itself, null
 * both for a null value and for a row not shown; rows are therefore taken
 * from the geometry leaves and attributes only looked up.
 */
const unwrap = (v: unknown): unknown => {
  if (!Array.isArray(v)) return v === null || v === undefined ? ABSENT : v
  return v.length ? v[0] : ABSENT
}

/** A uint32 key decoded as a signed int32 comes back negative above 2^31. */
const unsigned = (v: unknown): number => {
  const n = typeof v === 'bigint' ? Number(v) : (v as number)
  return n < 0 ? n + 2 ** 32 : n
}

/** GeoParquet: an omitted `crs` is OGC:CRS84; an explicit null is unknown. */
function isCrs84(column: { crs?: unknown } | undefined): boolean {
  if (!column || !('crs' in column)) return true
  const id = (column.crs as { id?: { authority?: unknown; code?: unknown } } | null)?.id
  const authority = String(id?.authority ?? '').toUpperCase()
  const code = String(id?.code ?? '')
  return (authority === 'OGC' && code === 'CRS84') || (authority === 'EPSG' && code === '4326')
}

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

/** hyparquet uses undefined for null optional structs inside repeated values. */
function normalizeNested(value: unknown): unknown {
  if (value == null) return null
  if (Array.isArray(value)) return Array.from(value, normalizeNested)
  if (typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype)
    return Object.fromEntries(
      Object.entries(value).map(([name, child]) => [name, normalizeNested(child)]),
    )
  return value
}
