import type { RangeSource } from './reader.js'

/**
 * Range requests round-robin over `urls`, alternate hostnames for one file.
 * Browsers allow six HTTP/1.1 connections per host, so spreading a tile's
 * requests over several hosts lets them run in fewer waves.
 *
 * Opening asks for the file's last `tail` bytes and, alongside, its size, so a
 * footer that fits the tail costs one round trip. The size comes from the tail
 * response's Content-Range when CORS exposes it, and from the HEAD otherwise
 * or when the server refuses suffix ranges.
 */
export const httpSource = async (
  urls: string[],
  { tail = 1024 * 1024, signal }: { tail?: number; signal?: AbortSignal } = {},
): Promise<RangeSource> => {
  const head = fetch(urls[0], { method: 'HEAD', cache: 'no-store', signal })
  head.catch(() => undefined)
  const response = await fetch(urls[0], {
    headers: { Range: `bytes=-${tail}` },
    cache: 'no-store',
    signal,
  })
  let byteLength = NaN
  let tailBuffer = new ArrayBuffer(0)
  if (response.status === 206) {
    byteLength = Number(/\/(\d+)\s*$/.exec(response.headers.get('Content-Range') ?? '')?.[1])
    tailBuffer = await response.arrayBuffer()
  } else await response.body?.cancel()
  if (!Number.isSafeInteger(byteLength)) {
    const h = await head
    byteLength = Number(h.headers.get('Content-Length'))
    if (!h.ok || !Number.isSafeInteger(byteLength))
      throw new Error(`cannot size ${urls[0]}: ${h.status}`)
  }
  const tailStart = byteLength - tailBuffer.byteLength
  let next = 0
  return {
    byteLength,
    async slice(start, end = byteLength, signal) {
      if (start >= tailStart && end <= byteLength)
        return tailBuffer.slice(start - tailStart, end - tailStart)
      const url = urls[next++ % urls.length]
      const response = await fetch(url, {
        headers: { Range: `bytes=${start}-${end - 1}` },
        cache: 'no-store',
        signal,
      })
      if (response.status !== 206) throw new Error(`range request failed: ${response.status}`)
      const buffer = await response.arrayBuffer()
      if (buffer.byteLength !== end - start) throw new Error(`truncated range from ${url}`)
      return buffer
    },
  }
}

/** The same S3 object under the hostnames the service answers on. A CDN makes this unnecessary. */
export const s3Mirrors = (url: string): string[] => {
  const match = /^https:\/\/([^./]+)\.s3\.([a-z0-9-]+)\.amazonaws\.com\/(.+)$/.exec(url)
  if (!match) return [url]
  const [, bucket, region, key] = match
  return [
    url,
    `https://s3.${region}.amazonaws.com/${bucket}/${key}`,
    `https://${bucket}.s3-${region}.amazonaws.com/${key}`,
  ]
}

interface Pending {
  start: number
  end: number
  signal?: AbortSignal
  resolve: (buffer: ArrayBuffer) => void
  reject: (error: unknown) => void
}

/**
 * Merge slices requested within one microtask when the bytes between them are
 * cheaper than another round trip. Page readers ask for pages one by one, and
 * the pages a tile needs from one column are usually short runs with gaps.
 */
export const coalescing = (
  source: RangeSource,
  { maxGap = 32 * 1024, minGap = 4 * 1024, gapRatio = 0.5, maxRun = 4 * 1024 * 1024 } = {},
): RangeSource => {
  let pending: Pending[] = []
  let scheduled = false

  const flush = () => {
    scheduled = false
    const batch = pending.sort((a, b) => a.start - b.start)
    pending = []
    let run: Pending[] = []
    let runStart = 0
    let runEnd = 0
    let useful = 0
    const send = () => {
      if (!run.length) return
      const members = run
      const [start, end] = [runStart, runEnd]
      const controller = new AbortController()
      const live = new Set(members.filter((m) => m.signal && !m.signal.aborted))
      for (const m of live) {
        m.signal!.addEventListener('abort', () => {
          live.delete(m)
          if (!live.size && members.every((x) => x.signal)) controller.abort()
        })
      }
      source.slice(start, end, members.every((m) => m.signal) ? controller.signal : undefined).then(
        (buffer) => {
          for (const m of members) {
            m.resolve(
              m.start === start && m.end === end
                ? buffer
                : buffer.slice(m.start - start, m.end - start),
            )
          }
        },
        (error) => members.forEach((m) => m.reject(error)),
      )
      run = []
      useful = 0
    }
    for (const p of batch) {
      // a gap is worth skipping only when it is small next to the bytes already wanted
      const gap = Math.min(maxGap, Math.max(minGap, useful * gapRatio))
      if (run.length && (p.start - runEnd > gap || Math.max(runEnd, p.end) - runStart > maxRun)) {
        send()
      }
      if (!run.length) runStart = p.start
      runEnd = run.length ? Math.max(runEnd, p.end) : p.end
      run.push(p)
      useful += p.end - p.start
    }
    send()
  }

  return {
    byteLength: source.byteLength,
    slice(start, end = source.byteLength, signal) {
      if (start === end) return Promise.resolve(new ArrayBuffer(0))
      if (signal?.aborted)
        return Promise.reject(signal.reason ?? new DOMException('Aborted', 'AbortError'))
      return new Promise<ArrayBuffer>((resolve, reject) => {
        pending.push({ start, end, signal, resolve, reject })
        if (!scheduled) {
          scheduled = true
          queueMicrotask(flush)
        }
      })
    },
  }
}
