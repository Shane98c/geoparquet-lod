import { MVT_BUFFER, MVT_EXTENT } from '@cogp/reader'
import type { Lod, LodReader, LodRows, PageLocation } from './reader.js'

const OPEN_BYTES = 65536

interface DirectoryLod {
  name: string
  zoom: number
  paths: string[]
  entries: number
  root_offset: number
  compressed_bytes: number
  raw_bytes: number
  blocks: number
}

export interface TileDirectoryHeader {
  experimental: boolean
  version: number
  file_size: number
  footer_length: number
  tile_size: number
  buffer: number
  slop_cells: number
  codec: string
  tile_order: string
  root_bytes: number
  lods: DirectoryLod[]
}

type Root = { first: number; last: number; offset: number; length: number }
type Entry = { tile: number; groups: Map<number, PageLocation[][]> }

class Varints {
  offset = 0
  constructor(readonly bytes: Uint8Array) {}
  uint(): number {
    let value = 0
    let multiplier = 1
    for (let i = 0; i < 8; i++) {
      if (this.offset >= this.bytes.length) throw new Error('truncated tile directory')
      const byte = this.bytes[this.offset++]
      value += (byte & 127) * multiplier
      if (!Number.isSafeInteger(value)) throw new Error('unsafe tile directory integer')
      if (byte < 128) return value
      multiplier *= 128
    }
    throw new Error('invalid tile directory varint')
  }
}

/** Morton intervals covering the buffered parent rectangle on the stored zoom's grid. */
export const tileIntervals = (
  finest: number,
  z: number,
  x: number,
  y: number,
): [number, number][] => {
  if (
    ![finest, z, x, y].every(Number.isInteger) ||
    z < 0 ||
    z > finest ||
    finest > 24 ||
    x < 0 ||
    y < 0 ||
    x >= 2 ** z ||
    y >= 2 ** z
  )
    throw new Error('unsupported tile coordinates')
  const n = 2 ** finest
  const factor = 2 ** (finest - z)
  const storedPadding = MVT_BUFFER / MVT_EXTENT
  const padding = storedPadding + (z < finest ? 1 / MVT_EXTENT : 0)
  const left = Math.max(0, Math.floor((x - padding) * factor + storedPadding))
  const right = Math.min(n - 1, Math.ceil((x + 1 + padding) * factor - storedPadding) - 1)
  const top = Math.max(0, Math.floor((y - padding) * factor + storedPadding))
  const bottom = Math.min(n - 1, Math.ceil((y + 1 + padding) * factor - storedPadding) - 1)
  const out: [number, number][] = []
  const visit = (tx: number, ty: number, size: number, id: number) => {
    if (tx > right || ty > bottom || tx + size <= left || ty + size <= top) return
    if (tx >= left && ty >= top && tx + size - 1 <= right && ty + size - 1 <= bottom) {
      const last = out[out.length - 1]
      if (last && last[1] + 1 === id) last[1] = id + size * size - 1
      else out.push([id, id + size * size - 1])
      return
    }
    const half = size / 2
    for (let child = 0; child < 4; child++) {
      visit(
        tx + (child % 2) * half,
        ty + Math.floor(child / 2) * half,
        half,
        id + child * half * half,
      )
    }
  }
  visit(0, 0, n, 0)
  return out
}

/** Experimental, version-bound sidecar accelerator. Geometry clipping uses the tile encoder. */
export class TileDirectory {
  private readonly roots = new Map<string, Root[]>()
  private readonly leaves = new Map<number, Promise<Entry[]>>()
  private readonly groupStarts: number[]

  private constructor(
    readonly reader: LodReader,
    readonly url: string,
    readonly header: TileDirectoryHeader,
    root: Uint8Array,
  ) {
    let start = 0
    this.groupStarts = reader.metadata.row_groups.map((group) => {
      const result = start
      start += Number(group.num_rows)
      return result
    })
    for (const lod of header.lods) {
      const input = new Varints(root)
      input.offset = lod.root_offset
      const count = input.uint()
      const blocks: Root[] = []
      let tile = 0
      let offset = 0
      for (let i = 0; i < count; i++) {
        tile += input.uint()
        const last = tile + input.uint()
        offset += input.uint()
        blocks.push({ first: tile, last, offset, length: input.uint() })
      }
      this.roots.set(lod.name, blocks)
    }
  }

  static async open(reader: LodReader, url: string, signal?: AbortSignal): Promise<TileDirectory> {
    const buffer = await fetchRange(url, 0, OPEN_BYTES, signal)
    const view = new DataView(buffer)
    const bytes = new Uint8Array(buffer)
    if (new TextDecoder().decode(bytes.subarray(0, 8)) !== 'LODTDIR1')
      throw new Error('invalid tile directory magic')
    const headerLength = view.getUint32(8, true)
    const rootLength = view.getUint32(12, true)
    if (16 + headerLength + rootLength > OPEN_BYTES)
      throw new Error('tile directory root exceeds open block')
    const header = JSON.parse(
      new TextDecoder().decode(bytes.subarray(16, 16 + headerLength)),
    ) as TileDirectoryHeader
    if (
      header.version !== 1 ||
      !header.experimental ||
      header.codec !== 'gzip' ||
      header.tile_order !== 'morton' ||
      header.tile_size !== 512 ||
      header.buffer !== MVT_BUFFER / MVT_EXTENT ||
      header.slop_cells !== 1 ||
      header.root_bytes !== rootLength
    )
      throw new Error('unsupported experimental tile directory')
    if (
      header.file_size !== reader.fileSize ||
      header.footer_length !== reader.metadata.metadata_length
    ) {
      throw new Error('tile directory file binding mismatch')
    }
    for (const lod of Object.values(reader.lods)) {
      const directory = header.lods.find((entry) => entry.name === lod.name)
      const paths = [
        reader.geometryLeaf(lod),
        ...reader.lod.attributes.map((a) => `${reader.lod.column}.${lod.name}.${a.name}`),
      ]
      if (
        !directory ||
        directory.zoom !== Math.max(...lod.zooms) ||
        directory.zoom > 24 ||
        JSON.stringify(directory.paths) !== JSON.stringify(paths)
      )
        throw new Error('tile directory LOD mismatch')
    }
    return new TileDirectory(
      reader,
      url,
      header,
      bytes.subarray(16 + headerLength, 16 + headerLength + rootLength),
    )
  }

  private leaf(block: Root, lod: DirectoryLod, signal?: AbortSignal): Promise<Entry[]> {
    let result = this.leaves.get(block.offset)
    if (!result) {
      result = (async () => {
        const buffer = await fetchRange(
          this.url,
          OPEN_BYTES + block.offset,
          OPEN_BYTES + block.offset + block.length,
          signal,
        )
        const stream = new Blob([buffer]).stream().pipeThrough(new DecompressionStream('gzip'))
        const input = new Varints(new Uint8Array(await new Response(stream).arrayBuffer()))
        const count = input.uint()
        const entries: Entry[] = []
        let tile = 0
        for (let i = 0; i < count; i++) {
          tile += input.uint()
          const groups = new Map<number, PageLocation[][]>()
          const groupCount = input.uint()
          for (let j = 0; j < groupCount; j++) {
            const g = input.uint()
            if (g >= this.reader.metadata.row_groups.length)
              throw new Error('invalid tile directory row group')
            const leaves = lod.paths.map(() => {
              const pages: PageLocation[] = []
              const pageCount = input.uint()
              let row = 0
              let offset = 0
              for (let p = 0; p < pageCount; p++) {
                row += input.uint()
                const rowEnd = row + input.uint()
                offset += input.uint()
                const byteEnd = offset + input.uint()
                if (
                  rowEnd <= row ||
                  rowEnd > Number(this.reader.metadata.row_groups[g].num_rows) ||
                  offset < 4 ||
                  byteEnd <= offset ||
                  byteEnd > this.header.file_size - this.header.footer_length - 8
                ) {
                  throw new Error('invalid tile directory page location')
                }
                pages.push({ rowStart: row, rowEnd, byteStart: offset, byteEnd })
              }
              return pages
            })
            groups.set(g, leaves)
          }
          entries.push({ tile, groups })
        }
        if (input.offset !== input.bytes.length) throw new Error('trailing tile directory bytes')
        return entries
      })()
      this.leaves.set(block.offset, result)
      result.catch(() => this.leaves.delete(block.offset))
    }
    return result
  }

  async readLodByTile(
    lod: Lod,
    z: number,
    x: number,
    y: number,
    attributes: string[] = [],
    signal?: AbortSignal,
  ): Promise<LodRows> {
    const directory = this.header.lods.find((entry) => entry.name === lod.name)
    if (!directory || z > directory.zoom) throw new Error('tile zoom exceeds directory LOD')
    const intervals = tileIntervals(directory.zoom, z, x, y)
    const overlaps = (first: number, last: number) =>
      intervals.some(([a, b]) => first <= b && last >= a)
    const blocks = this.roots.get(lod.name)!.filter((block) => overlaps(block.first, block.last))
    const entries = (
      await Promise.all(blocks.map((block) => this.leaf(block, directory, signal)))
    ).flat()
    signal?.throwIfAborted()
    const selected = [
      0,
      ...attributes.map((a) =>
        directory.paths.indexOf(`${this.reader.lod.column}.${lod.name}.${a}`),
      ),
    ]
    if (selected.includes(-1)) throw new Error('attribute not carried by tile directory')
    const groups = new Map<number, Map<number, PageLocation>[]>()
    for (const entry of entries) {
      if (!overlaps(entry.tile, entry.tile)) continue
      for (const [g, leaves] of entry.groups) {
        let group = groups.get(g)
        if (!group) groups.set(g, (group = selected.map(() => new Map())))
        selected.forEach((leaf, i) =>
          leaves[leaf].forEach((page) => group![i].set(page.byteStart, page)),
        )
      }
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

async function fetchRange(
  url: string,
  start: number,
  end: number,
  signal?: AbortSignal,
): Promise<ArrayBuffer> {
  const response = await fetch(url, {
    headers: { Range: `bytes=${start}-${end - 1}` },
    cache: 'no-store',
    signal,
  })
  if (response.status !== 206) throw new Error(`tile directory range failed: ${response.status}`)
  const buffer = await response.arrayBuffer()
  if (buffer.byteLength !== end - start) throw new Error('truncated tile directory range')
  return buffer
}

const directories = new WeakMap<LodReader, Map<string, Promise<TileDirectory>>>()

export function openTileDirectory(reader: LodReader, url: string): Promise<TileDirectory> {
  let cache = directories.get(reader)
  if (!cache) directories.set(reader, (cache = new Map()))
  let directory = cache.get(url)
  if (!directory) cache.set(url, (directory = TileDirectory.open(reader, url)))
  return directory
}
