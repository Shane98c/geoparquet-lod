// Retained-value budget, not a browser heap measurement. In-flight builds are
// deduplicated separately and never evicted while another tile is using them.
export class ByteCache<T> {
  values = new Map<string, { value: T; bytes: number }>()
  pending = new Map<string, Promise<T>>()
  bytes = 0
  constructor(readonly budget: number) {}
  get(key: string, build: () => Promise<T>): Promise<T> {
    const hit = this.values.get(key)
    if (hit) {
      this.values.delete(key)
      this.values.set(key, hit)
      return Promise.resolve(hit.value)
    }
    const pending = this.pending.get(key)
    if (pending) return pending
    const work = Promise.resolve()
      .then(build)
      .then((value) => {
        const bytes = retainedBytes(value)
        if (bytes <= this.budget) {
          while (this.bytes + bytes > this.budget && this.values.size) {
            const oldest = this.values.keys().next().value!
            this.bytes -= this.values.get(oldest)!.bytes
            this.values.delete(oldest)
          }
          this.values.set(key, { value, bytes })
          this.bytes += bytes
        }
        return value
      })
      .finally(() => this.pending.delete(key))
    this.pending.set(key, work)
    return work
  }
}

export function retainedBytes(value: unknown, seen = new Set<unknown>()): number {
  if (typeof value === 'string') return 40 + value.length * 2
  if (!value || typeof value !== 'object') return 8
  if (seen.has(value)) return 0
  seen.add(value)
  if (value instanceof ArrayBuffer) return value.byteLength
  // Count full backing allocations, including unused builder capacity.
  if (ArrayBuffer.isView(value)) return 64 + retainedBytes(value.buffer, seen)
  if (Array.isArray(value))
    return 32 + value.length * 8 + value.reduce((n, v) => n + retainedBytes(v, seen), 0)
  return 64 + Object.values(value).reduce<number>((n, v) => n + 8 + retainedBytes(v, seen), 0)
}
