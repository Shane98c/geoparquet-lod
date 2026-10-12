import type { RangeSource } from './reader.js'

/**
 * Range requests round-robin over `urls`, alternate hostnames for one file.
 * Browsers allow six HTTP/1.1 connections per host, so spreading a tile's
 * requests over several hosts lets them run in fewer waves.
 *
 * Opening asks for the file's size with a HEAD, its last `tail` bytes as an
 * explicit range, and when the footer is longer than that, the rest of the
 * footer exactly: two or three round trips, no suffix range, so it works
 * across origins wherever plain ranged GETs are allowed.
 */
/** Bytes read ahead of the footer when it must be fetched separately, for the page index just before it. */
const FOOTER_MARGIN = 64 * 1024

export const httpSource = async (
  urls: string[],
  { tail = 64 * 1024, signal }: { tail?: number; signal?: AbortSignal } = {},
): Promise<RangeSource> => {
  // HEAD for the size, then the tail as an explicit range: a suffix range (`bytes=-N`) is not a
  // CORS-safelisted header value, so a bucket whose rule allows only plain ranged GETs, as one
  // serving PMTiles does, would refuse its preflight
  const head = await fetch(urls[0], { method: 'HEAD', cache: 'no-store', signal })
  const byteLength = Number(head.headers.get('Content-Length'))
  if (!head.ok || !Number.isSafeInteger(byteLength))
    throw new Error(`cannot size ${urls[0]}: ${head.status}`)
  // probe the tail, read the footer's length from the file's last 8 bytes, and fetch exactly
  // what the footer still needs, so opening reads the footer and little else
  const get = async (start: number, end: number) => {
    const response = await fetch(urls[0], {
      headers: { Range: `bytes=${start}-${end - 1}` },
      cache: 'no-store',
      signal,
    })
    if (response.status !== 206) {
      await response.body?.cancel()
      throw new Error(`range request failed: ${response.status}`)
    }
    return response.arrayBuffer()
  }
  let tailStart = Math.max(0, byteLength - tail)
  let tailBuffer = byteLength > 0 ? await get(tailStart, byteLength) : new ArrayBuffer(0)
  if (tailBuffer.byteLength >= 8) {
    const view = new DataView(tailBuffer)
    const footerLength = view.getUint32(tailBuffer.byteLength - 8, true)
    const need = footerLength + 8 + FOOTER_MARGIN
    if (need > tailBuffer.byteLength && need <= byteLength) {
      const start = byteLength - need
      const rest = await get(start, tailStart)
      const joined = new Uint8Array(need)
      joined.set(new Uint8Array(rest), 0)
      joined.set(new Uint8Array(tailBuffer), rest.byteLength)
      tailBuffer = joined.buffer
      tailStart = start
    }
  }
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
