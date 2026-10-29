import { decompressPage } from 'hyparquet/src/datapage.js'
import { deserializeTCompactProtocol } from 'hyparquet/src/thrift.js'
import { compressors } from './compressors.js'

/** Visit only present values in a page of the profile's
 * required-struct/optional-binary column: a v1 data page of PLAIN values with
 * RLE definition levels and no repetition levels. Scan definition-level runs
 * directly, so sparse overviews don't allocate a null slot for every
 * analytical row. */
export function visitPlainPage(
  buffer: ArrayBuffer,
  codec: string,
  rows: number,
  visit: (row: number, value: Uint8Array) => void,
): void {
  const reader = { view: new DataView(buffer), offset: 0 }
  const header = deserializeTCompactProtocol(reader)
  const data = header.field_5 as Record<string, number> | undefined
  if (header.field_1 !== 0 || !data || data.field_2 !== 0 || data.field_3 !== 3)
    throw new Error('spatial LOD pages must be v1 data pages of PLAIN values')
  if (data.field_1 !== rows) throw new Error('spatial page row count mismatch')
  const compressedSize = Number(header.field_3)
  if (reader.offset + compressedSize !== buffer.byteLength) throw new Error('invalid page length')
  const bytes = decompressPage(
    new Uint8Array(buffer, reader.offset, compressedSize),
    Number(header.field_2),
    codec as never,
    compressors,
  )
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const definitionEnd = 4 + view.getUint32(0, true)
  if (definitionEnd > bytes.length) throw new Error('invalid definition level length')
  let definition = 4,
    value = definitionEnd,
    row = 0
  const emit = (at: number) => {
    const length = view.getUint32(value, true)
    value += 4
    if (value + length > bytes.length) throw new Error('truncated packed LOD value')
    visit(at, bytes.subarray(value, value + length))
    value += length
  }
  while (row < rows) {
    let run = 0,
      scale = 1,
      byte: number
    do {
      if (definition >= definitionEnd || scale > 2 ** 28) throw new Error('invalid definition run')
      byte = bytes[definition++]
      run += (byte & 127) * scale
      scale *= 128
    } while (byte & 128)
    if (run < 2) throw new Error('empty definition run')
    if (run % 2) {
      const groups = Math.floor(run / 2)
      if (definition + groups > definitionEnd) throw new Error('truncated definition bits')
      for (let group = 0; group < groups; group++) {
        const bits = bytes[definition++]
        const end = Math.min(rows, row + 8)
        if (bits) for (let bit = 0; row + bit < end; bit++) if (bits & (1 << bit)) emit(row + bit)
        row = end
      }
    } else {
      if (definition >= definitionEnd) throw new Error('truncated definition run')
      const present = bytes[definition++]
      if (present > 1) throw new Error('invalid optional binary definition level')
      const end = Math.min(rows, row + run / 2)
      if (present) for (let at = row; at < end; at++) emit(at)
      row = end
    }
  }
  if (definition !== definitionEnd || value !== bytes.length)
    throw new Error('invalid packed page payload length')
}
