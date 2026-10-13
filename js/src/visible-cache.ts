import type { DecodedTile } from './tile.js'
import { SharedLoad } from './shared-load.js'

/** One coordinator owns a bounded, source-qualified LRU of decoded overview
 * tiles. Pending loads survive until their last consumer cancels.
 * Typed buffer backing stores are counted in full; JS storage is estimated. */
export class VisibleTileCache {
  private entries = new Map<
    string,
    { task: SharedLoad<DecodedTile>; bytes: number; tile?: DecodedTile }
  >()
  bytes = 0
  constructor(
    readonly budget = 32 * 1024 * 1024,
    readonly limit = 64,
  ) {}
  get size() {
    return this.entries.size
  }
  getOrLoad(
    key: string,
    load: (signal: AbortSignal) => Promise<DecodedTile>,
    signal?: AbortSignal,
  ): Promise<DecodedTile> {
    if (signal?.aborted) return Promise.reject(signal.reason)
    const existing = this.entries.get(key)
    if (existing && !existing.task.abandoned) {
      this.entries.delete(key)
      this.entries.set(key, existing)
      return existing.task.get(signal)
    }
    if (existing) this.delete(key)
    const entry: { task: SharedLoad<DecodedTile>; bytes: number; tile?: DecodedTile } = {
      task: new SharedLoad(load),
      bytes: 0,
    }
    this.entries.set(key, entry)
    this.trim()
    entry.task.promise.then(
      (tile) => {
        if (this.entries.get(key) !== entry) return
        entry.tile = tile
        entry.bytes = decodedTileBytes(tile)
        this.bytes += entry.bytes
        if (entry.bytes > this.budget) this.delete(key)
        this.trim()
      },
      () => {
        if (this.entries.get(key) === entry) this.delete(key)
      },
    )
    return entry.task.get(signal)
  }
  addColumns(key: string, tile: DecodedTile, columns: Record<string, unknown[]>): void {
    const entry = this.entries.get(key)
    if (!entry || entry.tile !== tile) return
    const scalars = Object.fromEntries(
      Object.entries(columns).filter(([, values]) =>
        values.every(
          (value) =>
            value === null || ['string', 'number', 'boolean', 'bigint'].includes(typeof value),
        ),
      ),
    )
    tile.columns = { ...tile.columns, ...scalars }
    this.bytes -= entry.bytes
    entry.bytes = decodedTileBytes(tile)
    this.bytes += entry.bytes
    if (entry.bytes > this.budget) this.delete(key)
    this.trim()
  }
  private delete(key: string) {
    this.bytes -= this.entries.get(key)!.bytes
    this.entries.delete(key)
  }
  private trim() {
    while (this.entries.size && (this.entries.size > this.limit || this.bytes > this.budget))
      this.delete(this.entries.keys().next().value!)
  }
}
export function decodedTileBytes(tile: DecodedTile): number {
  const buffers = new Set<ArrayBufferLike>()
  for (const value of Object.values(tile.geometry))
    if (ArrayBuffer.isView(value)) buffers.add(value.buffer)
  let bytes = 512 + tile.rows.length * 8
  for (const buffer of buffers) bytes += buffer.byteLength
  for (const values of Object.values(tile.columns)) {
    bytes += 64 + values.length * 16
    for (const value of values) if (typeof value === 'string') bytes += 32 + value.length * 2
  }
  return bytes
}

export function waitForTile(
  promise: Promise<DecodedTile>,
  signal: AbortSignal,
): Promise<DecodedTile> {
  signal.throwIfAborted()
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException('Aborted', 'AbortError'))
    signal.addEventListener('abort', abort, { once: true })
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort))
  })
}
