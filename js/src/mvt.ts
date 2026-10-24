/*!
 * Adapted from @cogp/reader 2.1.4 (mvt.ts, clip.ts, properties.ts).
 * Streams MVT features using reusable buffers and clips only boundary paths.
 *
 * MIT License
 *
 * Copyright (c) 2026 Kanahiro Iguchi
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
import { clipPolygon, clipPolyline } from 'lineclip'
import type { FeatureSource, GeometryColumn } from '@cogp/reader'

/** Tile coordinate extent written by `toMvt`. */
export const MVT_EXTENT = 4096
/** Clip buffer around each tile, in `MVT_EXTENT` units. */
export const MVT_BUFFER = 64
const DEFAULT_LAYER_NAME = 'cogp'
const MVT_CLIP_BBOX: ClipBbox = [
  -MVT_BUFFER,
  -MVT_BUFFER,
  MVT_EXTENT + MVT_BUFFER,
  MVT_EXTENT + MVT_BUFFER,
]

const MAX_MERCATOR_LATITUDE = 85.0511287798066
const textEncoder = new TextEncoder()

export interface MvtTileOptions {
  z: number
  x: number
  y: number
  /** Default: `cogp`. */
  layer?: string
  /** Checked every 1024 rows. */
  signal?: AbortSignal
}

/**
 * Encode lon/lat rows as one Web Mercator MVT tile. Geometry is projected,
 * clipped to the buffered extent and dropped when null or off-tile. IDs must be
 * non-negative safe integers. Numbers, booleans and safe bigints are typed MVT
 * values; other properties are written as display strings.
 * Columns are read in place: no GeoJSON or per-row feature object is built.
 */
export function toMvt(source: FeatureSource, options: MvtTileOptions): ArrayBuffer {
  const geometry = source.geometry!
  const projection = new TileProjection(options.z, options.x, options.y)
  const columns = Object.entries(source.columns ?? {})
  const layer = new ByteWriter(65536)
  const commands = new ByteWriter()
  const tags = new ByteWriter(64)
  const keys = new Map<string, number>()
  const values = new Map<string, number>()
  const valueList: PropertyValue[] = []
  layer.writeVarintField(15, 2)
  layer.writeStringField(1, options.layer ?? DEFAULT_LAYER_NAME)
  for (let row = 0; row < geometry.length; row++) {
    if ((row & 1023) === 0) options.signal?.throwIfAborted()
    commands.reset()
    const feature = encodeGeometry(geometry, row, projection, source.rowIndex?.[row], commands)
    if (!feature) continue
    tags.reset()
    for (const [key, column] of columns) {
      const value = propertyValue(column[row])
      const id = `${value.field}:${value.value}`
      if (!keys.has(key)) keys.set(key, keys.size)
      if (!values.has(id)) {
        values.set(id, values.size)
        valueList.push(value)
      }
      tags.writeVarint(keys.get(key)!)
      tags.writeVarint(values.get(id)!)
    }
    writeFeature(layer, feature, tags.view())
  }
  options.signal?.throwIfAborted()
  for (const key of keys.keys()) layer.writeStringField(3, key)
  for (const value of valueList) {
    tags.reset()
    writePropertyValue(tags, value)
    layer.writeBytesField(4, tags.view())
  }
  layer.writeVarintField(5, MVT_EXTENT)
  const tile = new ByteWriter(layer.length + 16)
  tile.writeBytesField(3, layer.view())
  return tile.finish().buffer as ArrayBuffer
}

interface EncodedMvtFeature {
  id?: number
  type: 1 | 2 | 3
  geometry: Uint8Array
}

/**
 * Write MVT command integers directly from the column's coordinate arrays.
 * CogpReader selects candidate rows; reject distant parts here before
 * projecting and clipping their coordinates to the buffered tile extent.
 */
function encodeGeometry(
  column: GeometryColumn,
  row: number,
  projection: TileProjection,
  id: number | undefined,
  commands: ByteWriter,
): EncodedMvtFeature | null {
  const type = column.types[row]
  if (!type) return null
  const { geometryOffsets: go, polygonOffsets: po, ringOffsets: ro } = column
  const p0 = go[row]!,
    p1 = go[row + 1]!
  const c0 = ro[po[p0]!]!,
    c1 = ro[po[p1]!]!
  if (c0 === c1) return null
  const mvtType = type === 1 || type === 4 ? 1 : type === 2 || type === 5 ? 2 : 3
  const cursor = { x: 0, y: 0 }
  if (mvtType === 1) {
    if (!partIntersectsTile(column, projection, c0, c1)) return null
    writePoints(commands, column, projection, cursor, c0, c1)
  } else {
    for (let p = p0; p < p1; p++) {
      const firstRing = po[p]!,
        lastRing = po[p + 1]!
      if (
        mvtType === 3 &&
        (firstRing === lastRing ||
          !partIntersectsTile(column, projection, ro[firstRing]!, ro[lastRing]!))
      )
        continue
      for (let r = firstRing; r < lastRing; r++) {
        if (mvtType === 3)
          writePolygonRing(
            commands,
            column,
            projection,
            cursor,
            ro[r]!,
            ro[r + 1]!,
            r === firstRing,
          )
        else if (partIntersectsTile(column, projection, ro[r]!, ro[r + 1]!)) {
          writeLine(commands, column, projection, cursor, ro[r]!, ro[r + 1]!)
        }
      }
    }
  }
  return commands.length === 0 ? null : { id, type: mvtType, geometry: commands.view() }
}

/** Reject a part before projecting or clipping all its coordinates. */
function partIntersectsTile(
  column: GeometryColumn,
  projection: TileProjection,
  start: number,
  end: number,
): boolean {
  if (start >= end) return false
  let minX = Infinity,
    minY = Infinity,
    maxX = -Infinity,
    maxY = -Infinity
  for (let i = start; i < end; i++) {
    const x = column.x[i]!,
      y = column.y[i]!
    if (x < minX) minX = x
    if (x > maxX) maxX = x
    if (y < minY) minY = y
    if (y > maxY) maxY = y
  }
  // Leave non-finite inputs to the general projection and clipping path.
  if (
    !Number.isFinite(minX) ||
    !Number.isFinite(minY) ||
    !Number.isFinite(maxX) ||
    !Number.isFinite(maxY)
  )
    return true
  const west = column.offset[0] + column.scale[0] * minX
  const east = column.offset[0] + column.scale[0] * maxX
  if (!projection.intersectsLongitudeRange(Math.min(west, east), Math.max(west, east))) return false
  const south = projection.y(column.offset[1] + column.scale[1] * minY)
  const north = projection.y(column.offset[1] + column.scale[1] * maxY)
  return Math.max(south, north) >= MVT_CLIP_BBOX[1] && Math.min(south, north) <= MVT_CLIP_BBOX[3]
}

function writePoints(
  commands: ByteWriter,
  column: GeometryColumn,
  projection: TileProjection,
  cursor: { x: number; y: number },
  start: number,
  end: number,
): void {
  const coordinates: Position[] = []
  for (let i = start; i < end; i++) {
    const px = projectX(column, projection, i)
    const py = projectY(column, projection, i)
    if (!pointInBbox(px, py, MVT_CLIP_BBOX)) continue
    coordinates.push([px, py])
  }
  if (coordinates.length === 0) return
  commands.writeVarint(command(1, coordinates.length))
  for (const [px, py] of coordinates) {
    commands.writeVarint(zigZag(px - cursor.x))
    commands.writeVarint(zigZag(py - cursor.y))
    cursor.x = px
    cursor.y = py
  }
}

function writeLine(
  commands: ByteWriter,
  column: GeometryColumn,
  projection: TileProjection,
  cursor: { x: number; y: number },
  start: number,
  end: number,
): void {
  if (writeInteriorPath(commands, column, projection, cursor, start, end, false, false)) return
  const coordinates = projectPart(column, projection, start, end, false)
  for (const part of clipLineString(coordinates, MVT_CLIP_BBOX)) {
    writePath(commands, cursor, part, false)
  }
}

function writePolygonRing(
  commands: ByteWriter,
  column: GeometryColumn,
  projection: TileProjection,
  cursor: { x: number; y: number },
  start: number,
  end: number,
  exterior: boolean,
): void {
  if (writeInteriorPath(commands, column, projection, cursor, start, end, true, exterior)) return
  const coordinates = projectPart(column, projection, start, end, true)
  const clipped = clipPolygonRing(coordinates, MVT_CLIP_BBOX)
  // MVT uses clockwise exteriors in screen coordinates (Y points down).
  let area = 0
  for (let i = 0; i < clipped.length; i++) {
    const a = clipped[i]!
    const b = clipped[(i + 1) % clipped.length]!
    area += a[0] * b[1] - b[0] * a[1]
  }
  if (area > 0 !== exterior) clipped.reverse()
  writePath(commands, cursor, clipped, true)
}

/** Most LOD rings lie wholly inside the buffered tile. Project and normalize
 * those into reusable typed buffers, retaining the general clipping path for
 * boundary crossings. No coordinate-pair arrays are allocated here. */
function writeInteriorPath(
  commands: ByteWriter,
  column: GeometryColumn,
  projection: TileProjection,
  cursor: { x: number; y: number },
  start: number,
  end: number,
  closed: boolean,
  exterior: boolean,
): boolean {
  const repeatsFirst =
    closed &&
    end - start > 1 &&
    column.x[start] === column.x[end - 1] &&
    column.y[start] === column.y[end - 1]
  const limit = end - (repeatsFirst ? 1 : 0)
  projection.reservePath(limit - start)
  const xs = projection.pathX,
    ys = projection.pathY
  let count = 0,
    previousX: number | undefined
  for (let i = start; i < limit; i++) {
    const x = projectX(column, projection, i, previousX),
      y = projectY(column, projection, i)
    if (!pointInBbox(x, y, MVT_CLIP_BBOX)) return false
    previousX = x
    if (count && xs[count - 1] === x && ys[count - 1] === y) continue
    xs[count] = x
    ys[count++] = y
  }
  if (closed && count > 1 && xs[0] === xs[count - 1] && ys[0] === ys[count - 1]) count--
  if (count < (closed ? 3 : 2)) return true
  let reverse = false
  if (closed) {
    let area = 0
    for (let i = 0; i < count; i++) {
      const next = (i + 1) % count
      area += xs[i] * ys[next] - xs[next] * ys[i]
    }
    if (area === 0) return true
    reverse = area > 0 !== exterior
  }
  commands.writeVarint(command(1, 1))
  for (let k = 0; k < count; k++) {
    if (k === 1) commands.writeVarint(command(2, count - 1))
    const i = reverse ? count - 1 - k : k
    commands.writeVarint(zigZag(xs[i] - cursor.x))
    commands.writeVarint(zigZag(ys[i] - cursor.y))
    cursor.x = xs[i]
    cursor.y = ys[i]
  }
  if (closed) commands.writeVarint(command(7, 1))
  return true
}

function projectPart(
  column: GeometryColumn,
  projection: TileProjection,
  start: number,
  end: number,
  closed: boolean,
): Position[] {
  const repeatsFirst =
    closed &&
    end - start > 1 &&
    column.x[start] === column.x[end - 1] &&
    column.y[start] === column.y[end - 1]
  const limit = end - (repeatsFirst ? 1 : 0)
  const coordinates: Position[] = []
  let previousPartX: number | undefined
  for (let index = start; index < limit; index++) {
    const px = projectX(column, projection, index, previousPartX)
    coordinates.push([px, projectY(column, projection, index)])
    previousPartX = px
  }
  return coordinates
}

function writePath(
  commands: ByteWriter,
  cursor: { x: number; y: number },
  coordinates: readonly Position[],
  close: boolean,
): void {
  const count = coordinates.length
  if (count < (close ? 3 : 2)) return

  commands.writeVarint(command(1, 1))
  for (let i = 0; i < count; i++) {
    if (i === 1) commands.writeVarint(command(2, count - 1))
    const [px, py] = coordinates[i]!
    commands.writeVarint(zigZag(px - cursor.x))
    commands.writeVarint(zigZag(py - cursor.y))
    cursor.x = px
    cursor.y = py
  }
  if (close) commands.writeVarint(command(7, 1))
}

function projectX(
  column: GeometryColumn,
  projection: TileProjection,
  index: number,
  reference?: number,
): number {
  return projection.x(column.offset[0] + column.scale[0] * column.x[index]!, reference)
}

function projectY(column: GeometryColumn, projection: TileProjection, index: number): number {
  return projection.y(column.offset[1] + column.scale[1] * column.y[index]!)
}

function writeFeature(layer: ByteWriter, value: EncodedMvtFeature, tags: Uint8Array): void {
  // Protobuf requires the nested message length first. Its fields already have
  // known sizes, so write directly into the layer without a temporary buffer.
  const length =
    (value.id === undefined ? 0 : 1 + varintLength(value.id)) +
    (tags.byteLength ? 1 + varintLength(tags.byteLength) + tags.byteLength : 0) +
    1 +
    varintLength(value.type) +
    1 +
    varintLength(value.geometry.byteLength) +
    value.geometry.byteLength
  layer.writeVarint(2 * 8 + 2)
  layer.writeVarint(length)
  if (value.id !== undefined) layer.writeVarintField(1, value.id)
  if (tags.byteLength) layer.writeBytesField(2, tags)
  layer.writeVarintField(3, value.type)
  layer.writeBytesField(4, value.geometry)
}

function varintLength(value: number): number {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error('invalid protobuf varint')
  let length = 1
  while (value >= 0x80) {
    value = Math.floor(value / 0x80)
    length++
  }
  return length
}

function command(id: 1 | 2 | 7, count: number): number {
  return count * 8 + id
}

function zigZag(value: number): number {
  if (!Number.isSafeInteger(value) || Math.abs(value) > 0x7fff_ffff) {
    throw new Error('MVT coordinate delta exceeds signed 32-bit range')
  }
  return value < 0 ? -value * 2 - 1 : value * 2
}

class TileProjection {
  pathX = new Int32Array(256)
  pathY = new Int32Array(256)

  reservePath(length: number) {
    if (length <= this.pathX.length) return
    const size = Math.max(length, this.pathX.length * 2)
    this.pathX = new Int32Array(size)
    this.pathY = new Int32Array(size)
  }

  private readonly projectedY = new Map<number, number>()
  private readonly tiles: number
  private readonly tileX: number
  private readonly worldSize: number

  constructor(
    z: number,
    x: number,
    private readonly tileY: number,
  ) {
    this.tiles = 2 ** z
    this.tileX = ((x % this.tiles) + this.tiles) % this.tiles
    this.worldSize = this.tiles * MVT_EXTENT
  }

  x(longitude: number, reference = MVT_EXTENT / 2): number {
    let local = (((longitude + 180) / 360) * this.tiles - this.tileX) * MVT_EXTENT
    local += Math.round((reference - local) / this.worldSize) * this.worldSize
    return Math.round(local)
  }

  intersectsLongitudeRange(minimum: number, maximum: number): boolean {
    if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) return true
    // A ring crossing the antimeridian can follow the short path outside its
    // raw longitude interval; keep such wide intervals for exact clipping.
    if (maximum - minimum >= 180) return true
    const left = (((minimum + 180) / 360) * this.tiles - this.tileX) * MVT_EXTENT
    const right = (((maximum + 180) / 360) * this.tiles - this.tileX) * MVT_EXTENT
    // TileProjection.x can shift a part by whole worlds. Check every possible
    // shifted copy, with one pixel of slack for coordinate rounding.
    const first = Math.ceil((MVT_CLIP_BBOX[0] - 1 - right) / this.worldSize)
    const last = Math.floor((MVT_CLIP_BBOX[2] + 1 - left) / this.worldSize)
    return first <= last
  }

  y(latitude: number): number {
    const cached = this.projectedY.get(latitude)
    if (cached !== undefined) return cached
    const clamped = Math.max(-MAX_MERCATOR_LATITUDE, Math.min(MAX_MERCATOR_LATITUDE, latitude))
    const sin = Math.sin((clamped * Math.PI) / 180)
    const worldY = 0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)
    const result = Math.round((worldY * this.tiles - this.tileY) * MVT_EXTENT)
    if (this.projectedY.size < 65536) this.projectedY.set(latitude, result)
    return result
  }
}

class ByteWriter {
  private bytes: Uint8Array
  private offset = 0

  constructor(capacity = 256) {
    this.bytes = new Uint8Array(Math.max(16, capacity))
  }

  get length(): number {
    return this.offset
  }

  writeVarintField(field: number, value: number): void {
    this.writeVarint(field * 8)
    this.writeVarint(value)
  }

  writeStringField(field: number, value: string): void {
    this.writeBytesField(field, textEncoder.encode(value))
  }

  writeBytesField(field: number, value: Uint8Array): void {
    this.writeVarint(field * 8 + 2)
    this.writeVarint(value.byteLength)
    this.reserve(value.byteLength)
    this.bytes.set(value, this.offset)
    this.offset += value.byteLength
  }

  writeFloatField(field: number, value: number): void {
    this.writeVarint(field * 8 + 5)
    this.reserve(4)
    new DataView(this.bytes.buffer, this.bytes.byteOffset).setFloat32(this.offset, value, true)
    this.offset += 4
  }

  writeDoubleField(field: number, value: number): void {
    this.writeVarint(field * 8 + 1)
    this.reserve(8)
    new DataView(this.bytes.buffer, this.bytes.byteOffset).setFloat64(this.offset, value, true)
    this.offset += 8
  }

  writeVarint(value: number): void {
    if (!Number.isSafeInteger(value) || value < 0) throw new Error('invalid protobuf varint')
    this.reserve(10)
    while (value >= 0x80) {
      this.bytes[this.offset++] = (value % 0x80) + 0x80
      value = Math.floor(value / 0x80)
    }
    this.bytes[this.offset++] = value
  }

  reset(): void {
    this.offset = 0
  }

  view(): Uint8Array {
    return this.bytes.subarray(0, this.offset)
  }

  finish(): Uint8Array {
    return this.bytes.slice(0, this.offset)
  }

  private reserve(extra: number): void {
    const required = this.offset + extra
    if (required <= this.bytes.byteLength) return
    let capacity = this.bytes.byteLength
    while (capacity < required) capacity *= 2
    const grown = new Uint8Array(capacity)
    grown.set(this.bytes)
    this.bytes = grown
  }
}

type Position = [number, number]
type ClipBbox = [number, number, number, number]

function clipPolygonRing(input: Position[], bbox: ClipBbox): Position[] {
  if (input.length < 3) return []
  return normalizePath(clipPolygon(input, bbox), true)
}

function clipLineString(input: Position[], bbox: ClipBbox): Position[][] {
  if (input.length < 2) return []
  const output: Position[][] = []
  for (const part of clipPolyline(input, bbox)) {
    const normalized = normalizePath(part, false)
    if (normalized.length >= 2) output.push(normalized)
  }
  return output
}

function pointInBbox(x: number, y: number, bbox: ClipBbox): boolean {
  return x >= bbox[0] && x <= bbox[2] && y >= bbox[1] && y <= bbox[3]
}

function normalizePath(input: Position[], closed: boolean): Position[] {
  const output = input
  let count = 0
  for (let i = 0; i < input.length; i++) {
    const point = input[i]
    point[0] = Math.round(point[0])
    point[1] = Math.round(point[1])
    const previous = output[count - 1]
    if (previous?.[0] === point[0] && previous[1] === point[1]) continue
    output[count++] = point
  }
  output.length = count
  if (
    closed &&
    output.length >= 2 &&
    output[0]![0] === output[output.length - 1]![0] &&
    output[0]![1] === output[output.length - 1]![1]
  )
    output.pop()
  if (closed && (output.length < 3 || signedAreaTwice(output) === 0)) return []
  return output
}

function signedAreaTwice(coordinates: Position[]): number {
  let area = 0
  let previous = coordinates[coordinates.length - 1]!
  for (const coordinate of coordinates) {
    area += previous[0] * coordinate[1] - coordinate[0] * previous[1]
    previous = coordinate
  }
  return area
}

// MVT Value fields: 1 string, 2 float, 3 double, 6 sint64, 7 bool.
type PropertyValue =
  { field: 1; value: string } | { field: 2 | 3 | 6; value: number } | { field: 7; value: boolean }

// Integers whose zigzag encoding stays a safe integer.
const MAX_SINT = 2 ** 52

function propertyValue(value: unknown): PropertyValue {
  if (typeof value === 'number' && Number.isFinite(value)) {
    if (Number.isInteger(value) && Math.abs(value) < MAX_SINT) return { field: 6, value }
    // a float32 attribute arrives as a number that float storage keeps exactly
    return { field: Math.fround(value) === value ? 2 : 3, value }
  }
  // a bigint a number holds exactly is encoded as that number, so a value has
  // one type whether it comes from a carried attribute or a source column
  if (
    typeof value === 'bigint' &&
    value >= BigInt(Number.MIN_SAFE_INTEGER) &&
    value <= BigInt(Number.MAX_SAFE_INTEGER)
  )
    return propertyValue(Number(value))
  if (typeof value === 'boolean') return { field: 7, value }
  return { field: 1, value: formatPropertyValue(value) }
}

function writePropertyValue(writer: ByteWriter, value: PropertyValue): void {
  if (value.field === 1) writer.writeStringField(1, value.value)
  else if (value.field === 2) writer.writeFloatField(2, value.value)
  else if (value.field === 3) writer.writeDoubleField(3, value.value)
  else if (value.field === 6)
    writer.writeVarintField(6, value.value >= 0 ? value.value * 2 : -value.value * 2 - 1)
  else writer.writeVarintField(7, value.value ? 1 : 0)
}

function formatPropertyValue(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') {
    return String(value)
  }
  if (value instanceof Date) return value.toISOString()
  if (value instanceof Uint8Array) return `<bytes:${value.byteLength}>`
  try {
    return JSON.stringify(value, (_k, v) => {
      if (typeof v === 'bigint') return v.toString()
      if (v instanceof Map) return Object.fromEntries(v)
      return v
    })
  } catch {
    return String(value)
  }
}
