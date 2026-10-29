import { SharedLoad } from './shared-load.js'
import type { ProjectionExecutor } from './projection.js'
import { readColumn } from 'hyparquet/src/column.js'
import { DEFAULT_PARSERS } from 'hyparquet/src/convert.js'
import { getSchemaPath } from 'hyparquet/src/schema.js'
import { parquetMetadata } from 'hyparquet'
import { compressors } from './compressors.js'
import { attributeKind, decodeSelectedRow, type AttributeField } from './decode.js'
import { visitPlainPage } from './pages.js'
import { GeometryBuilder } from './geometry.js'
import { tileInterval } from './key.js'
import {
  LodReader,
  isCompatibleVersion,
  type Bbox,
  type Lod,
  type LodMetadata,
  type LodRows,
  type RangeSource,
} from './reader.js'

export const BOOTSTRAP_BYTES = 16384
const MAGIC = 'LODIDX01'
// [minimum key, maximum key, offset, length, first global row, row count, fences, valid key rows]
type Page = [number, number, number, number, number, number, number[], number?]
type Shard = [number, number, number, number, number?]
interface SpatialRoot {
  columns?: string[]
  bounds?: Bbox
  directory_encoding?: string
  version: number
  file_bytes: number
  footer_bytes?: number
  /** gzip Thrift metadata of the flat top-level columns, for row reads */
  row_metadata?: [number, number]
  lod: LodMetadata
  primary: string
  codecs: Record<string, string>
  /** One or more directory series per LOD; the reference writer emits one. */
  streams: Record<string, Shard[][]>
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
  if (root.version !== 1 || root.file_bytes !== file.byteLength)
    throw new Error('invalid spatial index version or file length')
  if (
    !isCompatibleVersion(root.lod.version) ||
    root.lod.encoding !== 'packed' ||
    root.lod.pack_version !== 1 ||
    !root.lod.key
  )
    throw new Error('unsupported spatial LOD encoding')
  if (root.directory_encoding === 'delta') {
    const encoded = root.streams as unknown as Record<string, [number, number, number[]][]>
    root.streams = Object.fromEntries(
      Object.entries(encoded).map(([name, sections]) => {
        const span = 4 ** Math.max(0, root.lod.key!.depth - Math.max(...root.lod.lods[name].zooms))
        const streams = sections.map(([base, depth, values]) => {
          const shards: Shard[] = []
          let offset = base,
            low = 0
          for (let i = 0; i < values.length; i += 3) {
            low += values[i]
            shards.push([
              low * span,
              (low + values[i + 1] + 1) * span - 1,
              offset,
              values[i + 2],
              depth,
            ])
            offset += values[i + 2]
          }
          return shards
        })
        return [name, streams]
      }),
    )
  } else if (root.directory_encoding !== undefined)
    throw new Error('unsupported directory encoding')
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
  private readonly directories = new Map<number, SharedLoad<Page[] | Shard[]>>()
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

  async readPrimary(bbox: Bbox, attributes: string[] = [], signal?: AbortSignal) {
    return (await this.analytical(signal)).readPrimary(bbox, attributes, signal)
  }

  private directory(shard: Shard, signal?: AbortSignal): Promise<Page[] | Shard[]> {
    const [, , offset, length] = shard
    let result = this.directories.get(offset)
    if (!result || result.abandoned) {
      result = new SharedLoad((signal) =>
        this.file.slice(offset, offset + length, signal).then(gunzipJson<Page[] | Shard[]>),
      )
      while (this.directories.size >= 128)
        this.directories.delete(this.directories.keys().next().value!)
      this.directories.set(offset, result)
      const current = result
      result.promise.catch(() => {
        if (this.directories.get(offset) === current) this.directories.delete(offset)
      })
    } else {
      this.directories.delete(offset)
      this.directories.set(offset, result)
    }
    return result.get(signal)
  }

  private async lookup(
    shards: Shard[],
    start: number,
    end: number,
    own: boolean,
    hosted: boolean,
    signal?: AbortSignal,
  ): Promise<Page[]> {
    const host = hosted ? (shards.find((s) => s[1] >= start) ?? shards.at(-1)) : undefined
    const hits = shards.filter((s) => (own && s[0] < end && s[1] >= start) || s === host)
    const selected = await Promise.all(
      hits.map(async (shard) => {
        const children = await this.directory(shard, signal)
        if (shard[4])
          return this.lookup(children as Shard[], start, end, own, shard === host, signal)
        const pages = children as Page[]
        const hostPage =
          shard === host ? (pages.find((p) => p[1] >= start) ?? pages.at(-1)) : undefined
        return pages.filter((p) => (own && p[0] < end && p[1] >= start) || p === hostPage)
      }),
    )
    return selected.flat()
  }

  private pageData(pages: Page[], signal?: AbortSignal): Promise<ArrayBuffer[]> {
    signal?.throwIfAborted()
    const runs: Page[][] = []
    for (const p of [...pages].sort((a, b) => a[2] - b[2])) {
      const cached = this.pages.get(p[2])
      if (cached && !cached.data.abandoned) continue
      if (cached) {
        this.pages.delete(p[2])
        this.pageBytes -= cached.size
      }
      const last = runs.at(-1)
      if (last && last.at(-1)![2] + last.at(-1)![3] === p[2]) last.push(p)
      else runs.push([p])
    }
    const wanted = new Map<number, Promise<ArrayBuffer>>()
    for (const p of pages) {
      const cached = this.pages.get(p[2])
      if (cached) {
        this.pages.delete(p[2])
        this.pages.set(p[2], cached)
        wanted.set(p[2], cached.data.get(signal))
      }
    }
    for (const run of runs) {
      const start = run[0][2],
        last = run.at(-1)!
      // Each page subscribes to the run; the last departing page aborts I/O.
      const fetched = new SharedLoad((signal) => this.file.slice(start, last[2] + last[3], signal))
      for (const p of run) {
        const data = new SharedLoad((signal) =>
          fetched.get(signal).then((bytes) => bytes.slice(p[2] - start, p[2] - start + p[3])),
        )
        wanted.set(p[2], data.get(signal))
        while (this.pages.size && this.pageBytes + p[3] > 16 * 1024 * 1024) {
          const oldest = this.pages.keys().next().value!
          this.pageBytes -= this.pages.get(oldest)!.size
          this.pages.delete(oldest)
        }
        if (p[3] <= 16 * 1024 * 1024) {
          this.pages.set(p[2], { size: p[3], data })
          this.pageBytes += p[3]
          data.promise.catch(() => {
            if (this.pages.get(p[2])?.data === data) {
              this.pages.delete(p[2])
              this.pageBytes -= p[3]
            }
          })
        }
      }
    }
    return Promise.all(pages.map((p) => wanted.get(p[2])!))
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
    const [start, end] = tileInterval(this.lod.key!.depth, z, x, y)
    const streams = this.root.streams[lod.name]
    if (!streams) throw new Error(`missing spatial directory for ${lod.name}`)
    // Copies are hosted in the last series; every series may hold a tile's own rows.
    let hostSection = 0
    streams.forEach((shards, section) => {
      if (shards.length) hostSection = section
    })
    const selected = await Promise.all(
      streams.map((shards, section) =>
        this.lookup(shards, start, end, true, section === hostSection, signal),
      ),
    )
    signal?.throwIfAborted()
    const pages = selected.flat().sort((a, b) => a[4] - b[4])
    const span = 4 ** Math.max(0, this.lod.key!.depth - Math.max(...lod.zooms))
    const firstCell = start / span,
      endCell = end / span
    const hostPage =
      selected[hostSection].find((p) => p[1] >= start) ?? selected[hostSection].at(-1)
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
    const payloads = await this.pageData(pages, signal)
    signal?.throwIfAborted()
    return {
      lod,
      z,
      x,
      y,
      fields: this.fields,
      column: this.lod.column,
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
  column: string
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
  const { lod, z, x, y, fields, column, codec, pages, firstCell, endCell, hostRow, payloads } = task
  const wanted = attributes.map((a) => fields.findIndex((f) => f.name === a))
  for (const [i, name] of attributes.entries())
    if (wanted[i] < 0) throw new Error(`attribute ${name} is not carried`)
  const rows: number[] = []
  const columns: Record<string, unknown[]> = Object.fromEntries(attributes.map((a) => [a, []]))
  const builder = new GeometryBuilder()
  const element = { name: lod.name, type: 'BYTE_ARRAY', repetition_type: 'OPTIONAL' }
  const decoder = {
    type: 'BYTE_ARRAY',
    codec,
    element,
    pathInSchema: [column, lod.name],
    schemaPath: getSchemaPath(
      [
        { name: 'schema', num_children: 1 },
        { name: column, repetition_type: 'REQUIRED', num_children: 1 },
        element,
      ] as never,
      [column, lod.name],
    ),
    compressors,
    parsers: DEFAULT_PARSERS,
    utf8: false,
  }
  const tile = z * 2 ** 48 + x * 2 ** z + y
  const add = (id: number, attrs: unknown[]) => {
    rows.push(id)
    attributes.forEach((a, i) => columns[a].push(attrs[wanted[i]]))
  }
  for (let i = 0; i < pages.length; i++) {
    const page = pages[i]
    let fence = 0
    const visit = (relative: number, value: Uint8Array) => {
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
    }
    if (visitPlainPage(payloads[i], codec, page[5], visit)) continue
    const { data } = readColumn(
      { view: new DataView(payloads[i]), offset: 0 },
      { groupStart: page[4], selectStart: 0, selectEnd: page[5] } as never,
      decoder as never,
    )
    let row = 0
    for (const values of data)
      for (let value of values) {
        while (Array.isArray(value)) value = value[0]
        if (value instanceof Uint8Array) visit(row, value)
        row++
      }
  }
  return { rows, columns, geometry: builder.finish(lod.scale, lod.offset) }
}
