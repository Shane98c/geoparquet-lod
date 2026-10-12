import { readColumn } from 'hyparquet/src/column.js'
import { DEFAULT_PARSERS } from 'hyparquet/src/convert.js'
import { getSchemaPath } from 'hyparquet/src/schema.js'
import { compressors } from 'hyparquet-compressors'
import { attributeKind, decodeRow, type AttributeField } from './decode.js'
import { tileInterval } from './key.js'
import {
  LodReader,
  type Bbox,
  type Lod,
  type LodMetadata,
  type LodRows,
  type RangeSource,
} from './reader.js'

export const BOOTSTRAP_BYTES = 16384
const MAGIC = 'LODIDX01'
// [minimum key, maximum key, offset, length, first global row, row count]
type Page = [number, number, number, number, number, number, number[], number?]
type Shard = [number, number, number, number, number?]
interface SpatialRoot {
  columns?: string[]
  bounds?: Bbox
  directory_encoding?: string
  version: number
  file_bytes: number
  lod: LodMetadata
  primary: string
  codecs: Record<string, string>
  streams: Record<string, [Shard[], Shard[]]>
}

export function hasSpatialIndex(bytes: ArrayBuffer): boolean {
  return bytes.byteLength >= 16 && new TextDecoder().decode(new Uint8Array(bytes, 4, 8)) === MAGIC
}

async function gunzipJson<T>(bytes: ArrayBuffer): Promise<T> {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))
  return JSON.parse(await new Response(stream).text()) as T
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
    root.lod.version !== '0.1.0' ||
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
        return [name, streams as [Shard[], Shard[]]]
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
  private readonly directories = new Map<number, Promise<Page[] | Shard[]>>()
  private readonly pages = new Map<number, { size: number; data: Promise<ArrayBuffer> }>()
  private pageBytes = 0
  private fallback?: Promise<LodReader>

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

  private analytical(): Promise<LodReader> {
    if (!this.fallback) {
      this.fallback = LodReader.open(this.file)
      this.fallback.catch(() => {
        this.fallback = undefined
      })
    }
    return this.fallback
  }

  async readRow(row: number, columns: string[], signal?: AbortSignal) {
    return (await this.analytical()).readRow(row, columns, signal)
  }

  async readPrimary(bbox: Bbox, attributes: string[] = [], signal?: AbortSignal) {
    return (await this.analytical()).readPrimary(bbox, attributes, signal)
  }

  private directory(shard: Shard): Promise<Page[] | Shard[]> {
    const [, , offset, length] = shard
    let result = this.directories.get(offset)
    if (!result) {
      result = this.file.slice(offset, offset + length).then(gunzipJson<Page[] | Shard[]>)
      while (this.directories.size >= 128)
        this.directories.delete(this.directories.keys().next().value!)
      this.directories.set(offset, result)
      const current = result
      result.catch(() => {
        if (this.directories.get(offset) === current) this.directories.delete(offset)
      })
    } else {
      this.directories.delete(offset)
      this.directories.set(offset, result)
    }
    return result
  }

  private async lookup(
    shards: Shard[],
    start: number,
    end: number,
    own: boolean,
    hosted: boolean,
  ): Promise<Page[]> {
    const host = hosted ? (shards.find((s) => s[1] >= start) ?? shards.at(-1)) : undefined
    const hits = shards.filter((s) => (own && s[0] < end && s[1] >= start) || s === host)
    const selected = await Promise.all(
      hits.map(async (shard) => {
        const children = await this.directory(shard)
        if (shard[4]) return this.lookup(children as Shard[], start, end, own, shard === host)
        const pages = children as Page[]
        const hostPage =
          shard === host ? (pages.find((p) => p[1] >= start) ?? pages.at(-1)) : undefined
        return pages.filter((p) => (own && p[0] < end && p[1] >= start) || p === hostPage)
      }),
    )
    return selected.flat()
  }

  private pageData(pages: Page[]): Promise<ArrayBuffer[]> {
    const runs: Page[][] = []
    for (const p of [...pages].sort((a, b) => a[2] - b[2])) {
      if (this.pages.has(p[2])) continue
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
        wanted.set(p[2], cached.data)
      }
    }
    for (const run of runs) {
      const start = run[0][2],
        last = run.at(-1)!
      // Shared requests outlive any one tile cancellation. Aborted callers are
      // checked before decoding; an aborted tile cannot poison its neighbours.
      const fetched = this.file.slice(start, last[2] + last[3])
      for (const p of run) {
        const data = fetched.then((bytes) => bytes.slice(p[2] - start, p[2] - start + p[3]))
        wanted.set(p[2], data)
        while (this.pages.size && this.pageBytes + p[3] > 16 * 1024 * 1024) {
          const oldest = this.pages.keys().next().value!
          this.pageBytes -= this.pages.get(oldest)!.size
          this.pages.delete(oldest)
        }
        if (p[3] <= 16 * 1024 * 1024) {
          this.pages.set(p[2], { size: p[3], data })
          this.pageBytes += p[3]
          data.catch(() => {
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
    signal?.throwIfAborted()
    const [start, end] = tileInterval(this.lod.key!.depth, z, x, y)
    const streams = this.root.streams[lod.name]
    if (!streams) throw new Error(`missing spatial directory for ${lod.name}`)
    const hostSection = streams[1].length ? 1 : 0
    const coarse = this.lod.prefix && Math.max(...lod.zooms) <= this.lod.prefix.zoom
    const selected = await Promise.all(
      streams.map((shards, section) =>
        this.lookup(shards, start, end, !(coarse && section === 1), section === hostSection),
      ),
    )
    signal?.throwIfAborted()
    const pages = selected.flat().sort((a, b) => a[4] - b[4])
    const prefixPages = new Set(selected[0].map((p) => p[2]))
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
    const wanted = attributes.map((a) => this.fields.findIndex((f) => f.name === a))
    if (wanted.includes(-1))
      throw new Error(`attribute ${attributes[wanted.indexOf(-1)]} is not carried`)
    const out: LodRows = {
      rows: [],
      coords: [],
      columns: Object.fromEntries(attributes.map((a) => [a, []])),
    }
    const payloads = await this.pageData(pages)
    signal?.throwIfAborted()
    const element = { name: lod.name, type: 'BYTE_ARRAY', repetition_type: 'OPTIONAL' }
    const decoder = {
      type: 'BYTE_ARRAY',
      codec: this.root.codecs[lod.name],
      element,
      pathInSchema: [this.lod.column, lod.name],
      schemaPath: getSchemaPath(
        [
          { name: 'schema', num_children: 1 },
          { name: this.lod.column, repetition_type: 'REQUIRED', num_children: 1 },
          element,
        ] as never,
        [this.lod.column, lod.name],
      ),
      compressors,
      parsers: DEFAULT_PARSERS,
      utf8: false,
    }
    const tile = z * 2 ** 48 + x * 2 ** z + y
    const add = (id: number, geometry: unknown, attrs: unknown[]) => {
      out.rows.push(id)
      out.coords.push(geometry)
      attributes.forEach((a, i) => out.columns[a].push(attrs[wanted[i]]))
    }
    for (let i = 0; i < pages.length; i++) {
      const page = pages[i]
      const { data } = readColumn(
        { view: new DataView(payloads[i]), offset: 0 },
        { groupStart: page[4], selectStart: 0, selectEnd: page[5] } as never,
        decoder as never,
      )
      let row = page[4]
      let fence = 0
      for (const values of data) {
        for (let value of values) {
          while (fence + 2 < page[6].length && page[6][fence + 3] <= row - page[4]) fence += 2
          const cell = page[6][fence]
          const own = (!coarse || prefixPages.has(page[2])) && cell >= firstCell && cell < endCell
          while (Array.isArray(value)) value = value[0]
          if ((own || row === hostRow) && value instanceof Uint8Array) {
            const decoded = decodeRow(value, lod.geometry_type, this.fields)
            if (own && (lod.geometry_type === 'Point' || (decoded.geometry as unknown[]).length))
              add(row, decoded.geometry, decoded.attrs)
            for (const copy of decoded.spill)
              if (copy.tile === tile) add(copy.id, copy.geometry, copy.attrs)
          }
          row++
        }
      }
    }
    return out
  }
}
