import { SharedLoad } from './shared-load.js'
import type { ProjectionExecutor, ProjectionTask } from './projection.js'
import { parquetMetadata, parquetSchema } from 'hyparquet'
import { readColumn } from 'hyparquet/src/column.js'
import { DEFAULT_PARSERS } from 'hyparquet/src/convert.js'
import { compressors } from './compressors.js'
import { attributeKind, decodeSelectedRow, type AttributeField } from './decode.js'
import { visitPlainPage } from './pages.js'
import { GeometryBuilder } from './geometry.js'
import { tileInterval } from './key.js'
import {
  LodReader,
  PACK_VERSION,
  isCompatibleVersion,
  type Bbox,
  type Lod,
  type LodMetadata,
  type LodRows,
  type PrimaryRows,
  type RangeSource,
} from './reader.js'
import { float32Outward, wkbBounds } from './wkb.js'

export const BOOTSTRAP_BYTES = 16384
const MAGIC = 'LODIDX01'
const VERSION = 3
const MAX_LEVEL = 32
/** Primary directory boxes are whole cells of 2^-12 degrees. */
const BOX_CELLS_PER_DEGREE = 4096
// [minimum key, maximum key, offset, length, first global row, row count, fences, valid key rows]
type Page = [number, number, number, number, number, number, number[], number?]
// [minimum key, maximum key, offset, length, level of the node located]
type Shard = [number, number, number, number, number]
/** A node's entries: pages in a leaf, else the nodes one level down. */
type DirectoryNode = { level: 0; entries: Page[] } | { level: number; entries: Shard[] }
/** Columns x0..x1 and rows y0..y1 of cells, inclusive. */
type Box = [number, number, number, number]
/** A page of a column chunk: byte range and global rows. */
interface SourcePage {
  offset: number
  length: number
  first: number
  count: number
}
/** A primary directory entry: a geometry page in a leaf, else a node one level down. */
interface BoxEntry {
  box: Box
  offset: number
  length: number
}
/** A geometry page; a page whose rows lie in distant places has pieces,
 * runs of its rows each with its own box. */
type ExactPage = BoxEntry & SourcePage & { pieces?: (SourcePage & { box: Box })[] }
/** A leaf adds, per indexed attribute, that column's pages over the leaf's rows. */
type BoxNode =
  { level: 0; entries: ExactPage[]; runs: SourcePage[][] } | { level: number; entries: BoxEntry[] }
interface PrimaryIndex {
  /** Carried attributes whose source pages each leaf locates */
  attributes: string[]
  /** Codecs of the primary geometry, then of each indexed attribute */
  codecs: string[]
  /** Base64 Thrift metadata whose schema holds those columns */
  schema: string
  /** The top node of the primary geometry's page tree */
  directory: unknown
}
/** What decodes one indexed column's pages without the footer. */
type ColumnDecoder = ProjectionTask['decoder'] & { codec: string }
interface SpatialRoot {
  columns?: string[]
  bounds?: Bbox
  version: number
  file_bytes: number
  footer_bytes?: number
  /** gzip Thrift metadata of the flat top-level columns, for row reads */
  row_metadata?: [number, number]
  lod: LodMetadata
  primary: string
  codecs: Record<string, string>
  /** The top node of each LOD's directory tree, at level one or above */
  directories: Record<string, unknown>
  primary_index: PrimaryIndex
}

const integers = (values: unknown): values is number[] =>
  Array.isArray(values) && values.every(Number.isSafeInteger)

/** Cells of a WGS84 bbox, rounded outward, as the primary directory's boxes are. */
const cellBox = ([west, south, east, north]: Bbox): Box => [
  Math.floor((west + 180) * BOX_CELLS_PER_DEGREE),
  Math.floor((south + 90) * BOX_CELLS_PER_DEGREE),
  Math.floor((east + 180) * BOX_CELLS_PER_DEGREE),
  Math.floor((north + 90) * BOX_CELLS_PER_DEGREE),
]

const overlaps = (a: Box | Bbox, b: Box | Bbox) =>
  a[0] <= b[2] && a[2] >= b[0] && a[1] <= b[3] && a[3] >= b[1]

/** Pages stored as `[gaps, lengths, rows, counts]`: each page's bytes after
 * the previous page's end, length, first row after the previous page's end
 * row, and rows, the first counted from zero. */
function decodeRuns(stored: unknown): SourcePage[] {
  const [gaps, lengths, rows, counts] = (Array.isArray(stored) ? stored : []) as number[][]
  if (
    !Array.isArray(stored) ||
    stored.length !== 4 ||
    ![gaps, lengths, rows, counts].every(
      (column) => integers(column) && column.length === gaps.length,
    )
  )
    throw new Error('malformed primary directory page run')
  const pages: SourcePage[] = []
  let end = 0,
    row = 0
  for (let i = 0; i < gaps.length; i++) {
    if (gaps[i] < 0 || lengths[i] <= 0 || rows[i] < 0 || counts[i] <= 0)
      throw new Error('malformed primary directory page run')
    const offset = end + gaps[i],
      first = row + rows[i]
    end = offset + lengths[i]
    row = first + counts[i]
    if (!Number.isSafeInteger(end) || !Number.isSafeInteger(row))
      throw new Error('malformed primary directory page run')
    pages.push({ offset, length: lengths[i], first, count: counts[i] })
  }
  return pages
}

/**
 * Decode a stored primary directory node: its level, then one column per
 * field with a value per entry. Every entry holds its minimum cell column and
 * row minus the previous entry's, its width and height in cells, its byte gap
 * after the previous entry's end, and its length; a leaf, at level 0, adds
 * each page's row gap after the previous page's end and row count, then one
 * page run per indexed attribute. The first entry counts from zero.
 */
function decodeBoxNode(stored: unknown, attributes: number): BoxNode {
  const node: unknown[] = Array.isArray(stored) ? stored : []
  const [level, wests, souths, widths, heights, gaps, lengths, rows, counts, pieces, runs] =
    node as [number, ...number[][]]
  if (
    !Number.isSafeInteger(level) ||
    level < 0 ||
    level > MAX_LEVEL ||
    node.length !== (level ? 7 : 11) ||
    ![wests, souths, widths, heights, gaps, lengths, ...(level ? [] : [rows, counts])].every(
      (column) => integers(column) && column.length === wests.length,
    ) ||
    (!level &&
      (!Array.isArray(pieces) ||
        pieces.length !== wests.length ||
        !Array.isArray(runs) ||
        runs.length !== attributes))
  )
    throw new Error('malformed primary directory node')
  const entries: ExactPage[] = []
  let x = 0,
    y = 0,
    end = 0,
    row = 0
  for (let i = 0; i < wests.length; i++) {
    if (
      widths[i] < 0 ||
      heights[i] < 0 ||
      gaps[i] < 0 ||
      lengths[i] <= 0 ||
      (!level && (rows[i] < 0 || counts[i] <= 0))
    )
      throw new Error('malformed primary directory node')
    x += wests[i]
    y += souths[i]
    const offset = end + gaps[i]
    end = offset + lengths[i]
    const first = level ? 0 : row + rows[i]
    row = level ? 0 : first + counts[i]
    if (![x, y, x + widths[i], y + heights[i], end, row].every(Number.isSafeInteger))
      throw new Error('malformed primary directory node')
    const box: Box = [x, y, x + widths[i], y + heights[i]]
    const page: ExactPage = { box, offset, length: lengths[i], first, count: level ? 0 : counts[i] }
    if (!level && (pieces as unknown as unknown[][])[i].length)
      page.pieces = decodePieces((pieces as unknown as unknown[])[i], page)
    entries.push(page)
  }
  if (level)
    return { level, entries: entries.map(({ box, offset, length }) => ({ box, offset, length })) }
  return { level: 0, entries, runs: (runs as unknown as unknown[]).map(decodeRuns) }
}

/** A page's pieces stored as `[rows, x, y, width, height, ...]` per piece,
 * x and y from the page box's minimum corner: two or more runs of its rows,
 * in order, each boxed within the page box. */
function decodePieces(stored: unknown, page: ExactPage): (SourcePage & { box: Box })[] {
  if (!integers(stored) || stored.length % 5 || stored.length < 10)
    throw new Error('malformed primary directory pieces')
  const out: (SourcePage & { box: Box })[] = []
  let row = page.first
  for (let i = 0; i < stored.length; i += 5) {
    const [count, dx, dy, width, height] = stored.slice(i, i + 5)
    const box: Box = [
      page.box[0] + dx,
      page.box[1] + dy,
      page.box[0] + dx + width,
      page.box[1] + dy + height,
    ]
    if (
      count <= 0 ||
      dx < 0 ||
      dy < 0 ||
      width < 0 ||
      height < 0 ||
      box[2] > page.box[2] ||
      box[3] > page.box[3]
    )
      throw new Error('malformed primary directory pieces')
    out.push({ box, offset: page.offset, length: page.length, first: row, count })
    row += count
  }
  if (row !== page.first + page.count) throw new Error('malformed primary directory pieces')
  return out
}

/** The box holding every entry's box. */
const union = (entries: BoxEntry[]): Box => {
  const out: Box = [Infinity, Infinity, -Infinity, -Infinity]
  for (const { box } of entries) {
    out[0] = Math.min(out[0], box[0])
    out[1] = Math.min(out[1], box[1])
    out[2] = Math.max(out[2], box[2])
    out[3] = Math.max(out[3], box[3])
  }
  return out
}

const RAW_GEOMETRY_PARSERS = {
  ...DEFAULT_PARSERS,
  geometryFromBytes: (bytes: Uint8Array) => bytes,
  geographyFromBytes: (bytes: Uint8Array) => bytes,
}

/** Values at the ascending global `rows` of consecutive `pages` of one
 * column, each page decoded whole; a page must hold exactly the rows the
 * directory says, and every requested row lie in a page. */
function decodePages(
  decoder: ColumnDecoder,
  pages: (SourcePage & { bytes: ArrayBuffer })[],
  rows: number[],
): unknown[] {
  const out = new Array<unknown>(rows.length)
  let next = 0
  for (const page of pages) {
    if (next === rows.length) break
    if (rows[next] < page.first) break
    if (rows[next] >= page.first + page.count) continue
    const { data } = readColumn(
      { view: new DataView(page.bytes), offset: 0 },
      { groupStart: page.first, selectStart: 0, selectEnd: page.count } as never,
      { ...decoder, compressors, parsers: RAW_GEOMETRY_PARSERS } as never,
    )
    let row = page.first
    for (const chunk of data)
      for (let i = 0; i < chunk.length; i++, row++)
        if (rows[next] === row) out[next++] = chunk[i] ?? null
    if (row !== page.first + page.count)
      throw new Error('page rows disagree with the primary directory')
  }
  if (next !== rows.length) throw new Error('primary directory pages miss requested rows')
  return out
}

/**
 * Decode a stored directory node: its level, then one column per field with a
 * value per entry. Every entry holds its minimum cell minus the previous
 * entry's, cell span, byte gap after the previous entry's end, and length; a
 * leaf, at level 0, adds the row gap after the previous page's end, row count,
 * fences after the implied [minimum cell, 0] as pairs minus the pair before,
 * and trailing null-key rows. The first entry counts from zero, and each
 * starts at or after the previous one's maximum cell. Bounds widen to whole
 * cells of `span` keys, which select the same entries for any tile interval
 * made of whole cells.
 */
function decodeNode(stored: unknown, span: number): DirectoryNode {
  const node: unknown[] = Array.isArray(stored) ? stored : []
  const [level, cells, spans, gaps, lengths, rows, counts, fences, nulls] = node as [
    number,
    number[],
    number[],
    number[],
    number[],
    number[],
    number[],
    number[][],
    number[],
  ]
  if (
    !Number.isSafeInteger(level) ||
    level < 0 ||
    level > MAX_LEVEL ||
    node.length !== (level ? 5 : 9) ||
    ![cells, spans, gaps, lengths, ...(level ? [] : [rows, counts, fences, nulls])].every(
      (column) => Array.isArray(column) && column.length === cells.length,
    ) ||
    ![cells, spans, gaps, lengths, ...(level ? [] : [rows, counts, nulls])].every(integers) ||
    (!level && !fences.every((f) => integers(f) && f.length % 2 === 0))
  )
    throw new Error('malformed spatial directory node')
  const pages: Page[] = [],
    shards: Shard[] = []
  let cell = 0,
    end = 0,
    row = 0
  for (let i = 0; i < cells.length; i++) {
    if (
      spans[i] < 0 ||
      gaps[i] < 0 ||
      lengths[i] <= 0 ||
      (!level && (rows[i] < 0 || counts[i] <= 0 || nulls[i] < 0 || nulls[i] >= counts[i]))
    )
      throw new Error('malformed spatial directory node')
    if (cells[i] < (i ? spans[i - 1] : 0))
      throw new Error('spatial directory entries out of key order')
    cell += cells[i]
    const offset = end + gaps[i]
    end = offset + lengths[i]
    const low = cell * span,
      high = (cell + spans[i] + 1) * span - 1
    if (level) {
      shards.push([low, high, offset, lengths[i], level - 1])
      continue
    }
    const first = row + rows[i]
    row = first + counts[i]
    const pairs = [cell, 0]
    for (let j = 0; j < fences[i].length; j += 2)
      pairs.push(pairs[j] + fences[i][j], pairs[j + 1] + fences[i][j + 1])
    pages.push([low, high, offset, lengths[i], first, counts[i], pairs, counts[i] - nulls[i]])
  }
  return level ? { level, entries: shards } : { level: 0, entries: pages }
}

export function hasSpatialIndex(bytes: ArrayBuffer): boolean {
  return bytes.byteLength >= 16 && new TextDecoder().decode(new Uint8Array(bytes, 4, 8)) === MAGIC
}

const gunzip = (bytes: ArrayBuffer): Response =>
  new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip')))

async function gunzipJson<T>(bytes: ArrayBuffer): Promise<T> {
  return JSON.parse(await gunzip(bytes).text()) as T
}

/** Parquet metadata from its Thrift bytes, framed as a footer for hyparquet. */
function metadataFrom(thrift: ArrayBuffer) {
  const footer = new Uint8Array(thrift.byteLength + 8)
  footer.set(new Uint8Array(thrift))
  new DataView(footer.buffer).setUint32(thrift.byteLength, thrift.byteLength, true)
  footer.set(new TextEncoder().encode('PAR1'), thrift.byteLength + 4)
  return parquetMetadata(footer.buffer)
}

/** The file length an indexed file's root records, from its leading bytes, or
 * null when `prefix` holds no readable root. */
export async function rootFileBytes(prefix: ArrayBuffer): Promise<number | null> {
  if (!hasSpatialIndex(prefix)) return null
  const length = new DataView(prefix).getUint32(12, true)
  if (!length || length > prefix.byteLength - 16) return null
  try {
    const root = await gunzipJson<Partial<SpatialRoot>>(prefix.slice(16, 16 + length))
    return Number.isSafeInteger(root.file_bytes) ? root.file_bytes! : null
  } catch {
    return null
  }
}

/** Common rendering/inspection API. Full Parquet metadata is loaded only when
 * an analytical row or the exact-geometry fallback is actually requested. */
export type TileReader = Pick<
  LodReader,
  | 'scalarColumns'
  | 'bounds'
  | 'lod'
  | 'lods'
  | 'primary'
  | 'finestZoom'
  | 'lodForZoom'
  | 'readLodByTile'
  | 'readPrimary'
  | 'readRow'
  | 'readColumns'
>

export async function openReader(file: RangeSource): Promise<TileReader> {
  const head = await file.slice(0, Math.min(BOOTSTRAP_BYTES, file.byteLength))
  if (!hasSpatialIndex(head)) return LodReader.open(file)
  const length = new DataView(head).getUint32(12, true)
  if (!length || length > head.byteLength - 16) throw new Error('invalid spatial index root length')
  const root = await gunzipJson<SpatialRoot>(head.slice(16, 16 + length))
  if (root.version !== VERSION) throw new Error(`unsupported spatial index version ${root.version}`)
  if (root.file_bytes !== file.byteLength)
    throw new Error('spatial index file length disagrees with the file')
  if (
    !isCompatibleVersion(root.lod.version) ||
    root.lod.encoding !== 'packed' ||
    root.lod.pack_version !== PACK_VERSION ||
    !root.lod.key
  )
    throw new Error('unsupported spatial LOD encoding')
  return new SpatialReader(file, root)
}

/** A directory over the original Parquet pages, not a second tile payload.
 * Only compressed pages are cached, bounded to 16 MiB; callers receive their
 * own decoded arrays. Neighbouring tiles share pending fetches and cached pages.
 */
export class SpatialReader implements TileReader {
  readonly bounds: Bbox
  readonly lod: LodMetadata
  readonly lods: Record<string, Lod>
  readonly primary: string
  readonly finestZoom: number
  private readonly fields: AttributeField[]
  private readonly trees: Record<string, DirectoryNode>
  private readonly exact: BoxNode
  /** Decoders of the primary geometry, then of each indexed attribute */
  private readonly decoders: ColumnDecoder[]
  /** Loaded directory nodes by offset, with the length they were read at */
  private readonly directories = new Map<
    number,
    { length: number; node: SharedLoad<DirectoryNode> }
  >()
  private readonly boxNodes = new Map<number, { length: number; node: SharedLoad<BoxNode> }>()
  private readonly pages = new Map<number, { size: number; data: SharedLoad<ArrayBuffer> }>()
  private pageBytes = 0
  private fallback?: SharedLoad<LodReader>
  private rowFallback?: SharedLoad<LodReader>

  constructor(
    private readonly file: RangeSource,
    private readonly root: SpatialRoot,
  ) {
    this.bounds = root.bounds ?? [-180, -85.051129, 180, 85.051129]
    this.lod = root.lod
    this.primary = root.primary
    this.finestZoom = Math.max(...this.lod.zooms)
    this.lods = Object.fromEntries(
      Object.entries(this.lod.lods).map(([name, lod]) => [name, { ...lod, name }]),
    )
    this.fields = this.lod.attributes.map((a) =>
      a.values
        ? { name: a.name, kind: 'enum', values: a.values }
        : { name: a.name, kind: attributeKind(a.type) },
    )
    this.trees = Object.fromEntries(
      Object.entries(root.directories ?? {}).map(([name, node]) => {
        if (!this.lods[name]) throw new Error(`spatial directory for unknown LOD ${name}`)
        const top = decodeNode(node, this.cellSpan(this.lods[name]))
        if (!top.level) throw new Error(`spatial directory for ${name} must start above its leaves`)
        return [name, top]
      }),
    )
    const index = root.primary_index
    const columns = [root.primary, ...(index?.attributes ?? [])]
    if (
      !Array.isArray(index?.attributes) ||
      !index.attributes.every((a) => typeof a === 'string') ||
      new Set(columns).size !== columns.length ||
      !Array.isArray(index.codecs) ||
      index.codecs.length !== columns.length ||
      typeof index.schema !== 'string'
    )
      throw new Error('malformed primary directory')
    this.exact = decodeBoxNode(index.directory, index.attributes.length)
    const schema = parquetSchema(
      metadataFrom(Uint8Array.from(atob(index.schema), (c) => c.charCodeAt(0)).buffer),
    )
    this.decoders = columns.map((name, i) => {
      const column = schema.children.find((c) => c.element.name === name)
      if (!column || column.children.length || !column.element.type)
        throw new Error(`primary directory schema lacks ${name}`)
      const { element } = column
      return {
        type: element.type!,
        codec: String(index.codecs[i]),
        element,
        schemaPath: [schema, column],
        pathInSchema: [name],
        utf8:
          i > 0 &&
          (element.converted_type === 'UTF8' ||
            (element.logical_type as { type?: string } | undefined)?.type === 'STRING'),
      }
    })
    if (this.decoders[0].type !== 'BYTE_ARRAY')
      throw new Error('the primary geometry is not a binary column')
  }

  /** Keys per directory cell: cells are at the LOD's finest zoom, at most the key depth. */
  private cellSpan(lod: Lod): number {
    return 4 ** Math.max(0, this.lod.key!.depth - Math.max(...lod.zooms))
  }

  lodForZoom(zoom: number): Lod {
    const target = Math.min(this.finestZoom, Math.max(Math.min(...this.lod.zooms), zoom))
    return Object.values(this.lods).find((lod) => lod.zooms.includes(target))!
  }

  scalarColumns(): string[] {
    return this.root.columns ?? this.lod.attributes.map((a) => a.name)
  }

  private analytical(signal?: AbortSignal): Promise<LodReader> {
    if (!this.fallback || this.fallback.abandoned) {
      const current = new SharedLoad((signal) =>
        LodReader.open(this.file, signal, this.root.footer_bytes),
      )
      this.fallback = current
      current.promise.catch(() => {
        if (this.fallback === current) this.fallback = undefined
      })
    }
    return this.fallback.get(signal)
  }

  /** A reader for `columns` of single rows: from the root's row metadata when
   * it covers them all, so the full footer is never fetched, else the footer. */
  private rowReader(columns: string[], signal?: AbortSignal): Promise<LodReader> {
    const covered = (c: string) => c === this.primary || this.root.columns?.includes(c)
    if (!this.root.row_metadata || !columns.every(covered)) return this.analytical(signal)
    if (!this.rowFallback || this.rowFallback.abandoned) {
      const [offset, length] = this.root.row_metadata
      const current = new SharedLoad(async (signal) => {
        const bytes = await this.file.slice(offset, offset + length, signal)
        const metadata = metadataFrom(await gunzip(bytes).arrayBuffer())
        signal?.throwIfAborted()
        return LodReader.fromMetadata(this.file, metadata)
      })
      this.rowFallback = current
      current.promise.catch(() => {
        if (this.rowFallback === current) this.rowFallback = undefined
      })
    }
    return this.rowFallback.get(signal)
  }

  async readRow(row: number, columns: string[], signal?: AbortSignal) {
    return (await this.rowReader(columns, signal)).readRow(row, columns, signal)
  }

  async readColumns(
    rows: number[],
    columns: string[],
    signal?: AbortSignal,
    execute?: ProjectionExecutor,
  ) {
    return (await this.rowReader(columns, signal)).readColumns(rows, columns, signal, execute)
  }

  /**
   * Rows whose geometry's bbox intersects `bbox`, with their exact primary
   * geometry as WKB and the named source attributes. The primary directory
   * selects the geometry pages whose boxes reach the query; their WKB gives
   * each row's extent, rounded outward to float32 as the covering stores it.
   * Indexed attributes come from the pages the same leaves locate, fetched
   * alongside the geometry; others through `readColumns`.
   */
  async readPrimary(
    bbox: Bbox,
    attributes: string[] = [],
    signal?: AbortSignal,
  ): Promise<PrimaryRows> {
    signal?.throwIfAborted()
    const indexed = this.root.primary_index.attributes
    const located = attributes.filter((a) => indexed.includes(a))
    const query = cellBox(bbox)
    const hits = await this.lookupBoxes(this.exact, query, new Set(), signal)
    const pages = hits.flatMap((hit) => hit.pages)
    // per located attribute, its pages over the rows of the selected geometry pages
    const sources = located.map((name) => {
      const column = indexed.indexOf(name)
      const wanted = new Map<number, SourcePage>()
      for (const hit of hits)
        for (const page of hit.runs[column])
          if (
            hit.pages.some(
              (p) => page.first < p.first + p.count && page.first + page.count > p.first,
            )
          )
            wanted.set(page.offset, page)
      return [...wanted.values()].sort((a, b) => a.first - b.first)
    })
    const [payloads, sourcePayloads] = await Promise.all([
      this.pageData(pages, signal),
      Promise.all(sources.map((list) => this.pageData(list, signal))),
    ])
    signal?.throwIfAborted()
    const out: PrimaryRows = {
      rows: [],
      wkb: [],
      columns: Object.fromEntries(attributes.map((a) => [a, []])),
    }
    // rows of pieces whose boxes miss the query have coverings that miss it too
    const live = pages.flatMap((page) =>
      (page.pieces ?? [page])
        .filter((piece) => overlaps(piece.box, query))
        .flatMap((piece) => Array.from({ length: piece.count }, (_, i) => piece.first + i)),
    )
    const wkb = decodePages(
      this.decoders[0],
      pages.map((page, i) => ({ ...page, bytes: payloads[i] })),
      live,
    )
    for (const [i, value] of wkb.entries()) {
      if (!(value instanceof Uint8Array)) continue
      const extent = wkbBounds(value)
      if (!extent || !overlaps(float32Outward(extent), bbox)) continue
      out.rows.push(live[i])
      out.wkb.push(value)
    }
    for (const [i, name] of located.entries())
      out.columns[name] = decodePages(
        this.decoders[indexed.indexOf(name) + 1],
        sources[i].map((page, j) => ({ ...page, bytes: sourcePayloads[i][j] })),
        out.rows,
      )
    const others = attributes.filter((a) => !located.includes(a))
    if (others.length) Object.assign(out.columns, await this.readColumns(out.rows, others, signal))
    signal?.throwIfAborted()
    return out
  }

  /** The geometry pages under a primary directory node whose boxes reach
   * `query`, with their leaf's attribute runs, in row order. `reached` holds
   * the offsets of the nodes already descended into; a tree reaches each node
   * once. */
  private async lookupBoxes(
    node: BoxNode,
    query: Box,
    reached: Set<number>,
    signal?: AbortSignal,
  ): Promise<{ pages: ExactPage[]; runs: SourcePage[][] }[]> {
    if ('runs' in node) {
      const pages = node.entries.filter(
        (page) =>
          overlaps(page.box, query) &&
          (!page.pieces || page.pieces.some((piece) => overlaps(piece.box, query))),
      )
      return pages.length ? [{ pages, runs: node.runs }] : []
    }
    const level = node.level
    const selected = await Promise.all(
      node.entries
        .filter((entry) => overlaps(entry.box, query))
        .map(async (entry) => {
          const child = await this.boxNode(entry, level - 1, signal)
          if (reached.has(entry.offset)) throw new Error('spatial directory node referenced twice')
          reached.add(entry.offset)
          return this.lookupBoxes(child, query, reached, signal)
        }),
    )
    return selected.flat()
  }

  /** A node from `cache` or loaded and decoded, read at `length` bytes. */
  private load<T>(
    cache: Map<number, { length: number; node: SharedLoad<T> }>,
    offset: number,
    length: number,
    decode: (stored: unknown) => T,
    signal?: AbortSignal,
  ): Promise<T> {
    let cached = cache.get(offset)
    if (!cached || cached.node.abandoned) {
      const current = {
        length,
        node: new SharedLoad(async (signal) =>
          decode(await gunzipJson(await this.file.slice(offset, offset + length, signal))),
        ),
      }
      while (cache.size >= 128) cache.delete(cache.keys().next().value!)
      cache.set(offset, current)
      current.node.promise.catch(() => {
        if (cache.get(offset) === current) cache.delete(offset)
      })
      cached = current
    } else {
      cache.delete(offset)
      cache.set(offset, cached)
    }
    if (cached.length !== length)
      return Promise.reject(new Error('spatial directory node disagrees with its parent entry'))
    return cached.node.get(signal)
  }

  /** The primary directory node an entry locates, which must be at `level`
   * with boxes whose union is the entry's box. */
  private async boxNode(entry: BoxEntry, level: number, signal?: AbortSignal): Promise<BoxNode> {
    const attributes = this.root.primary_index.attributes.length
    const node = await this.load(
      this.boxNodes,
      entry.offset,
      entry.length,
      (stored) => decodeBoxNode(stored, attributes),
      signal,
    )
    if (
      node.level !== level ||
      !node.entries.length ||
      union(node.entries).some((v, i) => v !== entry.box[i])
    )
      throw new Error('spatial directory node disagrees with its parent entry')
    return node
  }

  /** The node a parent's entry locates, which must match that entry's
   * length, level and cell bounds, from cache or loaded. */
  private async directory(
    shard: Shard,
    span: number,
    signal?: AbortSignal,
  ): Promise<DirectoryNode> {
    const [low, high, offset, length, level] = shard
    const node = await this.load(
      this.directories,
      offset,
      length,
      (stored) => decodeNode(stored, span),
      signal,
    )
    const entries: (Page | Shard)[] = node.entries
    if (
      node.level !== level ||
      !entries.length ||
      entries[0][0] !== low ||
      entries.at(-1)![1] !== high
    )
      throw new Error('spatial directory node disagrees with its parent entry')
    return node
  }

  /** The pages under `node` holding keys in [start, end), plus, when
   * `hosted`, the first page whose maximum key reaches `start`, or the last
   * page if none does, in key order. `reached` holds the offsets of the nodes
   * already descended into; a tree reaches each node once. */
  private async lookup(
    node: DirectoryNode,
    start: number,
    end: number,
    hosted: boolean,
    span: number,
    reached: Set<number>,
    signal?: AbortSignal,
  ): Promise<Page[]> {
    const entries: (Page | Shard)[] = node.entries
    const host = hosted ? (entries.find((e) => e[1] >= start) ?? entries.at(-1)) : undefined
    const hits = entries.filter((e) => (e[0] < end && e[1] >= start) || e === host)
    if (!node.level) return hits as Page[]
    const selected = await Promise.all(
      (hits as Shard[]).map(async (shard) => {
        const child = await this.directory(shard, span, signal)
        if (reached.has(shard[2])) throw new Error('spatial directory node referenced twice')
        reached.add(shard[2])
        return this.lookup(child, start, end, shard === host, span, reached, signal)
      }),
    )
    return selected.flat()
  }

  /** The bytes of each of `pages`, which may belong to any column, from the
   * compressed page cache or fetched; pages adjacent in the file share one
   * range. */
  private pageData(
    pages: { offset: number; length: number }[],
    signal?: AbortSignal,
  ): Promise<ArrayBuffer[]> {
    signal?.throwIfAborted()
    const runs: { offset: number; length: number }[][] = []
    for (const p of [...pages].sort((a, b) => a.offset - b.offset)) {
      const cached = this.pages.get(p.offset)
      if (cached && !cached.data.abandoned) continue
      if (cached) {
        this.pages.delete(p.offset)
        this.pageBytes -= cached.size
      }
      const last = runs.at(-1)?.at(-1)
      if (last && last.offset + last.length === p.offset) runs.at(-1)!.push(p)
      else if (!last || last.offset !== p.offset) runs.push([p])
    }
    const wanted = new Map<number, Promise<ArrayBuffer>>()
    for (const p of pages) {
      const cached = this.pages.get(p.offset)
      if (cached) {
        this.pages.delete(p.offset)
        this.pages.set(p.offset, cached)
        wanted.set(p.offset, cached.data.get(signal))
      }
    }
    for (const run of runs) {
      const start = run[0].offset,
        last = run.at(-1)!
      // Each page subscribes to the run; the last departing page aborts I/O.
      const fetched = new SharedLoad((signal) =>
        this.file.slice(start, last.offset + last.length, signal),
      )
      for (const p of run) {
        const data = new SharedLoad((signal) =>
          fetched
            .get(signal)
            .then((bytes) => bytes.slice(p.offset - start, p.offset - start + p.length)),
        )
        wanted.set(p.offset, data.get(signal))
        while (this.pages.size && this.pageBytes + p.length > 16 * 1024 * 1024) {
          const oldest = this.pages.keys().next().value!
          this.pageBytes -= this.pages.get(oldest)!.size
          this.pages.delete(oldest)
        }
        if (p.length <= 16 * 1024 * 1024) {
          this.pages.set(p.offset, { size: p.length, data })
          this.pageBytes += p.length
          data.promise.catch(() => {
            if (this.pages.get(p.offset)?.data === data) {
              this.pages.delete(p.offset)
              this.pageBytes -= p.length
            }
          })
        }
      }
    }
    return Promise.all(pages.map((p) => wanted.get(p.offset)!))
  }

  async readLodByTile(
    lod: Lod,
    z: number,
    x: number,
    y: number,
    attributes: string[] = [],
    signal?: AbortSignal,
  ): Promise<LodRows> {
    const carried = attributes.filter((a) => this.lod.attributes.some((f) => f.name === a))
    const source = attributes.filter((a) => !carried.includes(a))
    const prepared = await this.prepareTile(lod, z, x, y, signal)
    const out = decodeTile(prepared, carried, signal)
    if (source.length) Object.assign(out.columns, await this.readColumns(out.rows, source, signal))
    return out
  }

  async prepareTile(
    lod: Lod,
    z: number,
    x: number,
    y: number,
    signal?: AbortSignal,
  ): Promise<PreparedTile> {
    signal?.throwIfAborted()
    // Directory bounds and fences are whole cells of the LOD's finest zoom, so
    // they are exact only for tiles made of whole cells.
    if (z > Math.max(...lod.zooms)) throw new Error(`zoom ${z} is finer than ${lod.name}`)
    const [start, end] = tileInterval(this.lod.key!.depth, z, x, y)
    const tree = this.trees[lod.name]
    if (!tree) throw new Error(`missing spatial directory for ${lod.name}`)
    const span = this.cellSpan(lod)
    const pages = await this.lookup(tree, start, end, true, span, new Set(), signal)
    signal?.throwIfAborted()
    const firstCell = start / span,
      endCell = end / span
    const hostPage = pages.find((p) => p[1] >= start) ?? pages.at(-1)
    let hostRow = -1
    if (hostPage) {
      const fences = hostPage[6]
      const fence = fences.findIndex((value, i) => i % 2 === 0 && value >= firstCell)
      hostRow =
        hostPage[4] +
        (fence >= 0 && fences[fence + 1] < (hostPage[7] ?? hostPage[5])
          ? fences[fence + 1]
          : (hostPage[7] ?? hostPage[5]) - 1)
    }
    const payloads = await this.pageData(
      pages.map((p) => ({ offset: p[2], length: p[3] })),
      signal,
    )
    signal?.throwIfAborted()
    return {
      lod,
      z,
      x,
      y,
      fields: this.fields,
      codec: this.root.codecs[lod.name],
      pages,
      firstCell,
      endCell,
      hostRow,
      payloads,
    }
  }
}

export interface PreparedTile {
  lod: Lod
  z: number
  x: number
  y: number
  fields: AttributeField[]
  codec: string
  pages: Page[]
  firstCell: number
  endCell: number
  hostRow: number
  payloads: ArrayBuffer[]
}

/** Pure CPU stage: no reader, cache, or network access is needed in a tile worker. */
export function decodeTile(
  task: PreparedTile,
  attributes: string[],
  signal?: AbortSignal,
): LodRows {
  signal?.throwIfAborted()
  const { lod, z, x, y, fields, codec, pages, firstCell, endCell, hostRow, payloads } = task
  const wanted = attributes.map((a) => fields.findIndex((f) => f.name === a))
  for (const [i, name] of attributes.entries())
    if (wanted[i] < 0) throw new Error(`attribute ${name} is not carried`)
  const rows: number[] = []
  const columns: Record<string, unknown[]> = Object.fromEntries(attributes.map((a) => [a, []]))
  const builder = new GeometryBuilder()
  const tile = z * 2 ** 48 + x * 2 ** z + y
  const add = (id: number, attrs: unknown[]) => {
    rows.push(id)
    attributes.forEach((a, i) => columns[a].push(attrs[wanted[i]]))
  }
  for (let i = 0; i < pages.length; i++) {
    const page = pages[i]
    let fence = 0
    visitPlainPage(payloads[i], codec, page[5], (relative, value) => {
      const row = page[4] + relative
      while (fence + 2 < page[6].length && page[6][fence + 3] <= relative) fence += 2
      const cell = page[6][fence]
      const own = cell >= firstCell && cell < endCell
      if (!own && row !== hostRow) return
      decodeSelectedRow(
        value,
        lod.geometry_type,
        fields,
        builder,
        own,
        row,
        tile,
        add,
        wanted.length > 0,
      )
    })
  }
  return { rows, columns, geometry: builder.finish(lod.scale, lod.offset) }
}
