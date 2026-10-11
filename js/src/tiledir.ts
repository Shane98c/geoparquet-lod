import { MVT_BUFFER, MVT_EXTENT } from '@cogp/reader'
import {
  parquetMetadataAsync,
  parquetSchema,
  type AsyncBuffer,
  type FileMetaData,
  type SchemaTree,
} from 'hyparquet'
import { readColumn } from 'hyparquet/src/column.js'
import { DEFAULT_PARSERS } from 'hyparquet/src/convert.js'
import { getSchemaPath } from 'hyparquet/src/schema.js'
import { compressors } from 'hyparquet-compressors'
import type { Lod, LodReader, LodRows, PageLocation, RangeSource } from './reader.js'
import { coalescing } from './range-buffer.js'

export const TILE_DIRECTORY_PROFILE = 'parquet-table-1'
export const TILE_DIRECTORY_KEY = 'lod_tile_directory'
export const TILE_DIRECTORY_SUFFIX = '.tiledir.parquet'

export interface DirectoryEntry {
  lod: string
  zoom: number
  paths: string[]
  entries: number
}

/** The sidecar's footer metadata under `lod_tile_directory`. */
export interface TileDirectoryHeader {
  profile: string
  tile_size: number
  buffer: number
  slop_cells: number
  tile_order: string
  source: { file_size: number; footer_length: number; footer_sha256: string }
  directories: DirectoryEntry[]
}

const COLUMNS = ['zoom', 'tile', 'pages.list.element'] as const
const OPEN_BYTES = 64 * 1024

type Group = { zooms: number[]; tiles: number[]; pages: number[][] }

/** Morton id of tile (x, y) on zoom z's grid. */
export const tileId = (z: number, x: number, y: number): number => {
  if (
    ![z, x, y].every(Number.isInteger) ||
    z < 0 ||
    z > 24 ||
    x < 0 ||
    y < 0 ||
    x >= 2 ** z ||
    y >= 2 ** z
  )
    throw new Error('unsupported tile coordinates')
  let id = 0
  for (let i = 0; i < z; i++)
    id += ((x >> i) & 1) * 2 ** (2 * i) + ((y >> i) & 1) * 2 ** (2 * i + 1)
  return id
}

/**
 * A Parquet table kept beside the file, mapping (zoom, Morton tile id) to the LOD
 * pages a buffered tile needs. Rows are sorted by (zoom, tile); a lookup picks
 * row groups by footer statistics and reads one, so a tile skips the main
 * file's bbox and page index reads. Geometry clipping uses the tile encoder.
 */
export class TileDirectory {
  private readonly groupStarts: number[]
  private readonly cache = new Map<string, Promise<unknown>>()
  private readonly elements = new Map<string, SchemaTree['element']>()
  private readonly chunkIndex: number[]
  private readonly covered: Map<string, DirectoryEntry>

  private constructor(
    readonly reader: LodReader,
    private readonly file: RangeSource,
    readonly metadata: FileMetaData,
    readonly header: TileDirectoryHeader,
  ) {
    let start = 0
    this.groupStarts = reader.metadata.row_groups.map((group) => {
      const result = start
      start += Number(group.num_rows)
      return result
    })
    const visit = (node: SchemaTree) => {
      if (node.children.length) node.children.forEach(visit)
      else this.elements.set(node.path.join('.'), node.element)
    }
    visit(parquetSchema(metadata))
    const paths = metadata.row_groups[0]?.columns.map((c) => c.meta_data!.path_in_schema.join('.'))
    this.chunkIndex = COLUMNS.map((c) => paths?.indexOf(c) ?? 0)
    if (paths && this.chunkIndex.includes(-1)) throw new Error('tile directory schema mismatch')
    this.covered = new Map(header.directories.map((d) => [`${d.lod}/${d.zoom}`, d]))
  }

  /** Open a sidecar by URL or byte source and check that it belongs to `reader`'s file. */
  static async open(
    reader: LodReader,
    source: string | RangeSource,
    signal?: AbortSignal,
  ): Promise<TileDirectory> {
    const file = coalescing(typeof source === 'string' ? await tailSource(source, signal) : source)
    const metadata = await parquetMetadataAsync(file as unknown as AsyncBuffer, {
      initialFetchSize: OPEN_BYTES,
    })
    const raw = metadata.key_value_metadata?.find((kv) => kv.key === TILE_DIRECTORY_KEY)?.value
    if (!raw) throw new Error(`tile directory has no ${TILE_DIRECTORY_KEY} metadata`)
    const header = JSON.parse(raw) as TileDirectoryHeader
    if (
      header.profile !== TILE_DIRECTORY_PROFILE ||
      header.tile_order !== 'morton' ||
      header.tile_size !== 512 ||
      header.buffer !== MVT_BUFFER / MVT_EXTENT ||
      header.slop_cells !== 1
    )
      throw new Error(`unsupported tile directory profile ${header.profile}`)
    if (
      header.source.file_size !== reader.fileSize ||
      header.source.footer_length !== reader.metadata.metadata_length ||
      header.source.footer_sha256 !== (await reader.footerDigest())
    )
      throw new Error('tile directory file binding mismatch')
    for (const entry of header.directories) {
      const lod = reader.lods[entry.lod]
      if (!lod || !lod.zooms.includes(entry.zoom) || entry.zoom > 24)
        throw new Error('tile directory LOD mismatch')
      const paths = [
        reader.geometryLeaf(lod),
        ...reader.lod.attributes.map((a) => `${reader.lod.column}.${lod.name}.${a.name}`),
      ]
      if (JSON.stringify(entry.paths) !== JSON.stringify(paths))
        throw new Error('tile directory leaf paths mismatch')
    }
    return new TileDirectory(reader, file, metadata, header)
  }

  /** Whether the directory serves `lod` at zoom `z`, or at any zoom when `z` is omitted. */
  covers(lod: Lod, z?: number): boolean {
    if (z !== undefined) return this.covered.has(`${lod.name}/${z}`)
    return this.header.directories.some((entry) => entry.lod === lod.name)
  }

  async readLodByTile(
    lod: Lod,
    z: number,
    x: number,
    y: number,
    attributes: string[] = [],
    signal?: AbortSignal,
  ): Promise<LodRows> {
    const directory = this.covered.get(`${lod.name}/${z}`)
    if (!directory) throw new Error('tile zoom has no tile directory')
    const flat = await this.lookup(z, tileId(z, x, y))
    signal?.throwIfAborted()
    const selected = [
      0,
      ...attributes.map((a) =>
        directory.paths.indexOf(`${this.reader.lod.column}.${lod.name}.${a}`),
      ),
    ]
    if (selected.includes(-1)) throw new Error('attribute not carried by tile directory')
    const groups = new Map<number, Map<number, PageLocation>[]>()
    if (flat.length % 6) throw new Error('invalid tile directory page list')
    const dataEnd = this.reader.fileSize - this.reader.metadata.metadata_length - 8
    for (let i = 0; i < flat.length; i += 6) {
      const [g, leaf, rowStart, rowCount, byteStart, byteLength] = flat.slice(i, i + 6)
      if (
        g >= this.reader.metadata.row_groups.length ||
        leaf >= directory.paths.length ||
        rowCount <= 0 ||
        rowStart + rowCount > Number(this.reader.metadata.row_groups[g].num_rows) ||
        byteStart < 4 ||
        byteLength <= 0 ||
        byteStart + byteLength > dataEnd
      )
        throw new Error('invalid tile directory page location')
      if (!selected.includes(leaf)) continue
      let group = groups.get(g)
      if (!group) groups.set(g, (group = selected.map(() => new Map())))
      const page = {
        rowStart,
        rowEnd: rowStart + rowCount,
        byteStart,
        byteEnd: byteStart + byteLength,
      }
      selected.forEach((s, j) => {
        if (s === leaf) group![j].set(byteStart, page)
      })
    }
    const output: LodRows = {
      rows: [],
      coords: [],
      columns: Object.fromEntries(attributes.map((a) => [a, []])),
    }
    const decoded = await Promise.all(
      [...groups]
        .sort(([a], [b]) => a - b)
        .map(async ([g, leaves]) => ({
          g,
          values: await Promise.all(
            leaves.map((pages, i) =>
              this.reader.readLocatedPages(
                g,
                directory.paths[selected[i]],
                [...pages.values()],
                signal,
              ),
            ),
          ),
        })),
    )
    for (const {
      g,
      values: [coords, ...attrs],
    } of decoded) {
      for (const [row, value] of [...coords].sort(([a], [b]) => a - b)) {
        if (!geometryIntersects(value, lod, z, x, y)) continue
        output.rows.push(this.groupStarts[g] + row)
        output.coords.push(value)
        attributes.forEach((a, i) => output.columns[a].push(attrs[i].get(row) ?? null))
      }
    }
    return output
  }

  /** The flat page sextuples of tile `t` at zoom `z`; empty when the tile has no row. */
  private async lookup(z: number, t: number): Promise<number[]> {
    const stat = (g: number, c: number) => {
      const s = this.metadata.row_groups[g].columns[this.chunkIndex[c]].meta_data!.statistics
      return s ? [Number(s.min_value), Number(s.max_value)] : [-Infinity, Infinity]
    }
    const candidates = this.metadata.row_groups
      .map((_, g) => g)
      .filter((g) => {
        const [z0, z1] = stat(g, 0)
        const [t0, t1] = stat(g, 1)
        return z0 <= z && z <= z1 && t0 <= t && t <= t1
      })
    const found = await Promise.all(
      candidates.map((g) => this.wholeGroup(g).then((group) => findRow(group, z, t))),
    )
    return found.find((pages) => pages !== null) ?? []
  }

  private groupRange(g: number): [number, number] {
    const chunks = this.metadata.row_groups[g].columns.map((c) => c.meta_data!)
    const start = Math.min(
      ...chunks.map((m) => Number(m.dictionary_page_offset ?? m.data_page_offset)),
    )
    const end = Math.max(
      ...chunks.map(
        (m) =>
          Number(m.dictionary_page_offset ?? m.data_page_offset) + Number(m.total_compressed_size),
      ),
    )
    return [start, end]
  }

  /** A whole row group in one range, decoded once. */
  private wholeGroup(g: number): Promise<Group> {
    return this.cached(`g:${g}`, async () => {
      const [start, end] = this.groupRange(g)
      const buffer = await this.file.slice(start, end)
      const rows = Number(this.metadata.row_groups[g].num_rows)
      const [zooms, tiles, pages] = COLUMNS.map((leaf, c) => {
        const m = this.metadata.row_groups[g].columns[this.chunkIndex[c]].meta_data!
        const offset = Number(m.dictionary_page_offset ?? m.data_page_offset) - start
        const view = new DataView(buffer, offset, Number(m.total_compressed_size))
        return this.decode(g, leaf, view, rows)
      })
      return {
        zooms: zooms.map(Number),
        tiles: tiles.map(Number),
        pages: pages.map((p) => Array.from(p as ArrayLike<bigint>, Number)),
      }
    })
  }

  private decode(g: number, leaf: string, view: DataView, rows: number): unknown[] {
    const meta =
      this.metadata.row_groups[g].columns[this.chunkIndex[COLUMNS.indexOf(leaf as never)]]
        .meta_data!
    const { data } = readColumn(
      { view, offset: 0 },
      { groupStart: 0, groupRows: rows, selectStart: 0, selectEnd: rows } as never,
      {
        ...meta,
        pathInSchema: leaf.split('.'),
        element: this.elements.get(leaf)!,
        schemaPath: getSchemaPath(this.metadata.schema, leaf.split('.')),
        compressors,
        parsers: DEFAULT_PARSERS,
      } as never,
    )
    return (data as ArrayLike<unknown>[]).flatMap((chunk) => Array.from(chunk))
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

const findRow = (group: Group, z: number, t: number): number[] | null => {
  let lo = 0
  let hi = group.tiles.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    const before = group.zooms[mid] < z || (group.zooms[mid] === z && group.tiles[mid] < t)
    if (before) lo = mid + 1
    else hi = mid
  }
  return lo < group.tiles.length && group.zooms[lo] === z && group.tiles[lo] === t
    ? group.pages[lo]
    : null
}

/** Bounds of decoded display coordinates avoid a separate bbox-column read. */
function geometryIntersects(value: unknown, lod: Lod, z: number, x: number, y: number): boolean {
  let xmin = Infinity,
    ymin = Infinity,
    xmax = -Infinity,
    ymax = -Infinity
  const visit = (node: unknown): void => {
    if (!Array.isArray(node)) return
    if (node.length === 2 && typeof node[0] === 'number') {
      const lon = lod.offset[0] + lod.scale[0] * node[0]
      const lat = lod.offset[1] + lod.scale[1] * node[1]
      xmin = Math.min(xmin, lon)
      xmax = Math.max(xmax, lon)
      ymin = Math.min(ymin, lat)
      ymax = Math.max(ymax, lat)
    } else node.forEach(visit)
  }
  visit(value)
  if (![xmin, ymin, xmax, ymax].every(Number.isFinite)) return true
  const n = 2 ** z
  const left = (((xmin + 180) / 360) * n - x) * MVT_EXTENT
  const right = (((xmax + 180) / 360) * n - x) * MVT_EXTENT
  const first = Math.ceil((-MVT_BUFFER - 1 - right) / (n * MVT_EXTENT))
  const last = Math.floor((MVT_EXTENT + MVT_BUFFER + 1 - left) / (n * MVT_EXTENT))
  if (xmax - xmin < 180 && first > last) return false
  const projectY = (lat: number) => {
    const clamped = Math.max(-85.0511287798066, Math.min(85.0511287798066, lat))
    const sin = Math.sin((clamped * Math.PI) / 180)
    return Math.round(
      ((0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * n - y) * MVT_EXTENT,
    )
  }
  return projectY(ymin) >= -MVT_BUFFER && projectY(ymax) <= MVT_EXTENT + MVT_BUFFER
}

/**
 * A range source whose first request is a suffix range for the footer, so
 * opening needs no HEAD. The size comes from Content-Range, or from a HEAD
 * when the server hides that header or refuses suffix ranges.
 */
async function tailSource(url: string, signal?: AbortSignal): Promise<RangeSource> {
  const response = await fetch(url, {
    headers: { Range: `bytes=-${OPEN_BYTES}` },
    cache: 'no-store',
    signal,
  })
  let size = NaN
  let tail = new ArrayBuffer(0)
  if (response.status === 206) {
    size = Number(/\/(\d+)\s*$/.exec(response.headers.get('Content-Range') ?? '')?.[1])
    tail = await response.arrayBuffer()
  } else await response.body?.cancel()
  if (!Number.isSafeInteger(size)) {
    const head = await fetch(url, { method: 'HEAD', cache: 'no-store', signal })
    size = Number(head.headers.get('Content-Length'))
    if (!head.ok || !Number.isSafeInteger(size)) throw new Error('cannot size tile directory')
  }
  const tailStart = size - tail.byteLength
  return {
    byteLength: size,
    async slice(start, end = size, signal) {
      if (start >= tailStart) return tail.slice(start - tailStart, end - tailStart)
      const r = await fetch(url, {
        headers: { Range: `bytes=${start}-${end - 1}` },
        cache: 'no-store',
        signal,
      })
      if (r.status !== 206) throw new Error(`tile directory range failed: ${r.status}`)
      const buffer = await r.arrayBuffer()
      if (buffer.byteLength !== end - start) throw new Error('truncated tile directory range')
      return buffer
    },
  }
}

const directories = new WeakMap<LodReader, Map<string, Promise<TileDirectory>>>()

export function openTileDirectory(reader: LodReader, url: string): Promise<TileDirectory> {
  let cache = directories.get(reader)
  if (!cache) directories.set(reader, (cache = new Map()))
  let directory = cache.get(url)
  if (!directory) cache.set(url, (directory = TileDirectory.open(reader, url)))
  return directory
}
