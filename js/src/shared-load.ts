/** A pending cache entry owns its I/O; callers own independent subscriptions.
 * Only the last departing caller aborts the load. Abandoned entries must be
 * replaced on lookup, even if their old I/O has not rejected yet. */
export class SharedLoad<T> {
  private controller = new AbortController()
  private users = 0
  private settled = false
  private begin?: () => void
  readonly promise: Promise<T>
  get abandoned(): boolean {
    return this.controller.signal.aborted
  }
  constructor(load: (signal: AbortSignal) => Promise<T>) {
    // Lazy dispatch matters for page runs: page subscribers are created first,
    // and may all disappear before any of them starts the underlying range.
    this.promise = new Promise((resolve, reject) => {
      this.begin = () => {
        Promise.resolve()
          .then(() => {
            this.controller.signal.throwIfAborted()
            return load(this.controller.signal)
          })
          .then((value) => {
            this.controller.signal.throwIfAborted()
            return value
          })
          .then(resolve, reject)
      }
    })
    this.promise.then(
      () => {
        this.settled = true
      },
      () => {
        this.settled = true
      },
    )
  }
  get(signal?: AbortSignal): Promise<T> {
    if (signal?.aborted) return Promise.reject(signal.reason)
    if (this.abandoned) return Promise.reject(this.controller.signal.reason)
    this.users++
    return new Promise((resolve, reject) => {
      let done = false
      const leave = () => {
        if (done) return false
        done = true
        signal?.removeEventListener('abort', abort)
        if (--this.users === 0 && !this.settled) this.controller.abort()
        return true
      }
      const abort = () => {
        if (leave()) reject(signal!.reason)
      }
      signal?.addEventListener('abort', abort, { once: true })
      const begin = this.begin
      this.begin = undefined
      begin?.()
      this.promise.then(
        (value) => {
          if (leave()) resolve(value)
        },
        (error) => {
          if (leave()) reject(error)
        },
      )
    })
  }
}
