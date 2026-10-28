import { readColumn } from 'hyparquet/src/column.js'
import { DEFAULT_PARSERS } from 'hyparquet/src/convert.js'
import { compressors } from './compressors.js'
import type { SchemaTree } from 'hyparquet'

/** Serializable source-page work. Row IDs are sorted and local to a row group. */
export interface ProjectionTask {
  rows: number[]
  decoder: {
    type: string
    codec?: string
    element: SchemaTree['element']
    schemaPath: unknown
    pathInSchema: string[]
    utf8: boolean
  }
  dictionary: ArrayBuffer | null
  runs: { first: number; count: number; bytes: ArrayBuffer }[]
}
export type ProjectionExecutor = (task: ProjectionTask, signal?: AbortSignal) => Promise<unknown[]>

const parsers = {
  ...DEFAULT_PARSERS,
  geometryFromBytes: (bytes: Uint8Array) => bytes,
  geographyFromBytes: (bytes: Uint8Array) => bytes,
}

/** Keep only requested rows, avoiding a Map of every decoded source value. */
export function decodeProjection(task: ProjectionTask): unknown[] {
  const out = new Array<unknown>(task.rows.length).fill(null)
  let selected = 0
  for (const run of task.runs) {
    let buffer = run.bytes
    if (task.dictionary) {
      const joined = new Uint8Array(task.dictionary.byteLength + buffer.byteLength)
      joined.set(new Uint8Array(task.dictionary))
      joined.set(new Uint8Array(buffer), task.dictionary.byteLength)
      buffer = joined.buffer
    }
    const { data } = readColumn(
      { view: new DataView(buffer), offset: 0 },
      { groupStart: run.first, selectStart: 0, selectEnd: run.count } as never,
      { ...task.decoder, compressors, parsers } as never,
    )
    let row = run.first
    for (const chunk of data) {
      while (selected < task.rows.length && task.rows[selected] < row + chunk.length) {
        const index = task.rows[selected] - row
        if (index < 0) throw new Error('source projection page coverage is incomplete')
        out[selected++] = chunk[index] ?? null
      }
      row += chunk.length
    }
  }
  if (selected !== task.rows.length)
    throw new Error('source projection page coverage is incomplete')
  return out
}
