import { decompressPage, readDataPage } from '../../node_modules/hyparquet/src/datapage.js'
import { deserializeTCompactProtocol } from '../../node_modules/hyparquet/src/thrift.js'
import { getMaxDefinitionLevel } from '../../node_modules/hyparquet/src/schema.js'
import { Encodings } from '../../node_modules/hyparquet/src/constants.js'
import { compressors } from 'hyparquet-compressors'
import { GeometryBuilder } from '../../src/geometry.js'

// Specialization for the compact builder's three LIST levels and int32 XY
// leaves. Other schemas/encodings/null structures use the ordinary reader.
function coordinatePages(buffer: ArrayBuffer, schemaPath: any[]) {
  const pattern = schemaPath
    .slice(1)
    .map((p) => p.element.repetition_type)
    .join(',')
  const element = schemaPath.at(-1)!.element
  if (
    element.type !== 'INT32' ||
    pattern !== 'OPTIONAL,REPEATED,OPTIONAL,REPEATED,OPTIONAL,REPEATED,OPTIONAL,OPTIONAL'
  )
    return
  const reader = { view: new DataView(buffer), offset: 0 }
  const pages: { values: Int32Array; reps: number[] }[] = []
  while (reader.offset < buffer.byteLength) {
    const header = deserializeTCompactProtocol(reader)
    const d = header.field_5
    // Our current writer uses V1 delta pages without a dictionary. Do not
    // silently reinterpret dictionary/V2 pages or null/empty geometries.
    if (
      header.field_1 !== 0 ||
      !d ||
      Encodings[d.field_2] !== 'DELTA_BINARY_PACKED' ||
      Encodings[d.field_3] !== 'RLE' ||
      Encodings[d.field_4] !== 'RLE'
    )
      return
    const size = header.field_3
    if (!Number.isInteger(size) || size < 0 || reader.offset + size > buffer.byteLength)
      throw Error('Truncated coordinate page')
    const bytes = new Uint8Array(buffer, reader.offset, size)
    reader.offset += size
    const decoded = readDataPage(
      decompressPage(bytes, header.field_2, 'ZSTD', compressors),
      {
        num_values: d.field_1,
        encoding: 'DELTA_BINARY_PACKED',
        definition_level_encoding: 'RLE',
        repetition_level_encoding: 'RLE',
      },
      { type: 'INT32', element, schemaPath } as any,
    )
    const maxDef = getMaxDefinitionLevel(schemaPath)
    if (decoded.definitionLevels?.some((def: number) => def !== maxDef)) return
    const values = decoded.dataPage as Int32Array,
      reps = decoded.repetitionLevels
    if (values.length !== reps.length) throw Error('Coordinate level/value mismatch')
    pages.push({ values, reps })
  }
  return pages
}

export function directCoordinates(
  xBuffer: ArrayBuffer,
  yBuffer: ArrayBuffer,
  xPath: any[],
  yPath: any[],
  rows: number,
) {
  const x = coordinatePages(xBuffer, xPath),
    y = coordinatePages(yBuffer, yPath)
  if (!x || !y) return
  const builder = new GeometryBuilder()
  let yp = 0,
    yi = 0
  for (const page of x)
    for (let i = 0; i < page.values.length; i++) {
      while (yp < y.length && yi === y[yp].values.length) {
        yp++
        yi = 0
      }
      const rep = page.reps[i]
      if (yp >= y.length || rep !== y[yp].reps[yi] || rep < 0 || rep > 3)
        throw Error('XY repetition mismatch')
      if (!builder.length && rep !== 0) throw Error('Missing first geometry boundary')
      if (rep === 0) builder.begin('MultiPolygon')
      if (rep <= 1) builder.startPolygon()
      if (rep <= 2) builder.startRing()
      const at = builder.reserveCoordinates(1)
      builder.x[at] = page.values[i]
      builder.y[at] = y[yp].values[yi++]
    }
  while (yp < y.length && yi === y[yp].values.length) {
    yp++
    yi = 0
  }
  if (yp !== y.length || builder.length !== rows) throw Error('Coordinate row/count mismatch')
  return builder
}
