import type { RangeSource } from './reader.js'
import { rootFileBytes } from './spatial.js'

/**
 * Range requests round-robin over `urls`, alternate hostnames for one file.
 * Browsers allow six HTTP/1.1 connections per host, so spreading a tile's
 * requests over several hosts lets them run in fewer waves.
 *
 * Indexed files open with one leading 16 KiB GET. Unindexed files then fetch the
 * tail and, if needed, the remainder of the footer. Content-Range supplies the
 * size; HEAD is needed only when CORS does not expose that response header.
 */
/** Bytes read ahead of the footer when it must be fetched separately, for the page index just before it. */
const FOOTER_MARGIN = 64 * 1024

export const httpSource = async (
  urls: string[],
  { tail = 64 * 1024, signal }: { tail?: number; signal?: AbortSignal } = {},
): Promise<RangeSource> => {
  if (!urls.length) throw new Error('at least one URL is required')
  // A fixed leading range opens indexed files in one request. Unindexed files
  // use Content-Range for their size, then continue through the normal footer.
  const first = await fetch(urls[0], {
    headers: { Range: 'bytes=0-16383' },
    signal,
  })
  if (first.status !== 206) {
    await first.body?.cancel()
    throw new Error(`range request failed: ${first.status}`)
  }
  const prefix = await first.arrayBuffer()
  const total = first.headers.get('Content-Range')?.match(/\/(\d+)$/)?.[1]
  let byteLength = total ? Number(total) : NaN
  // when CORS hides Content-Range, an indexed file's root records its length;
  // that is the file's own claim, so it is checked before any data is used
  let sizedByRoot = false
  if (!Number.isSafeInteger(byteLength)) {
    const fromRoot = await rootFileBytes(prefix)
    if (fromRoot !== null) {
      byteLength = fromRoot
      sizedByRoot = true
    }
  }
  if (!Number.isSafeInteger(byteLength)) {
    const head = await fetch(urls[0], { method: 'HEAD', signal })
    byteLength = Number(head.headers.get('Content-Length'))
    if (!head.ok || !head.headers.has('Content-Length')) throw new Error(`cannot size ${urls[0]}`)
  }
  if (
    !Number.isSafeInteger(byteLength) ||
    byteLength < 8 ||
    prefix.byteLength !== Math.min(16384, byteLength)
  )
    throw new Error(`invalid range length from ${urls[0]}`)
  const indexed =
    prefix.byteLength >= 16 && new TextDecoder().decode(new Uint8Array(prefix, 4, 8)) === 'LODIDX01'
  // probe the tail, read the footer's length from the file's last 8 bytes, and fetch exactly
  // what the footer still needs, so opening reads the footer and little else
  const get = async (start: number, end: number) => {
    const response = await fetch(urls[0], {
      headers: { Range: `bytes=${start}-${end - 1}` },
      signal,
    })
    if (response.status !== 206) {
      await response.body?.cancel()
      throw new Error(`range request failed: ${response.status}`)
    }
    const buffer = await response.arrayBuffer()
    if (buffer.byteLength !== end - start) throw new Error(`truncated range from ${urls[0]}`)
    return buffer
  }
  let tailStart = indexed ? byteLength : Math.max(0, byteLength - tail)
  let tailBuffer = indexed
    ? new ArrayBuffer(0)
    : tailStart === 0 && prefix.byteLength === byteLength
      ? prefix
      : await get(tailStart, byteLength)
  if (tailBuffer.byteLength >= 8) {
    const view = new DataView(tailBuffer)
    const footerLength = view.getUint32(tailBuffer.byteLength - 8, true)
    const need = footerLength + 8 + FOOTER_MARGIN
    if (footerLength + 8 > tailBuffer.byteLength && need <= byteLength) {
      const start = byteLength - need
      const rest = await get(start, tailStart)
      const joined = new Uint8Array(need)
      joined.set(new Uint8Array(rest), 0)
      joined.set(new Uint8Array(tailBuffer), rest.byteLength)
      tailBuffer = joined.buffer
      tailStart = start
    }
  }
  // A root-sized file must end in the Parquet magic exactly at that length. The
  // check runs beside the first read past the bootstrap, not before it, and
  // without the opening signal, so one cancelled read cannot fail it for all.
  let verified: Promise<void> | null = sizedByRoot ? null : Promise.resolve()
  const verify = () =>
    (verified ??= fetch(urls[0], {
      headers: { Range: `bytes=${byteLength - 8}-${byteLength - 1}` },
    }).then(async (response) => {
      const bytes = response.status === 206 ? new Uint8Array(await response.arrayBuffer()) : null
      if (
        !bytes ||
        bytes.byteLength !== 8 ||
        new TextDecoder().decode(bytes.subarray(4)) !== 'PAR1'
      )
        throw new Error(`${urls[0]} does not end where its spatial index says`)
    }))
  let next = 0
  return {
    byteLength,
    async slice(start, end = byteLength, signal) {
      signal?.throwIfAborted()
      if (
        !Number.isSafeInteger(start) ||
        !Number.isSafeInteger(end) ||
        start < 0 ||
        end < start ||
        end > byteLength
      )
        throw new Error('invalid byte range')
      if (start === end) return new ArrayBuffer(0)
      if (start >= 0 && end <= prefix.byteLength) return prefix.slice(start, end)
      if (start >= tailStart && end <= byteLength)
        return tailBuffer.slice(start - tailStart, end - tailStart)
      const url = urls[next++ % urls.length]
      const read = async () => {
        const response = await fetch(url, {
          headers: { Range: `bytes=${start}-${end - 1}` },
          signal,
        })
        if (response.status !== 206) {
          await response.body?.cancel()
          throw new Error(`range request failed: ${response.status}`)
        }
        const buffer = await response.arrayBuffer()
        if (buffer.byteLength !== end - start) throw new Error(`truncated range from ${url}`)
        return buffer
      }
      const [buffer] = await Promise.all([read(), verify()])
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
 * Merge slices requested within one microtask (or a short configured window) when the bytes between them are
 * cheaper than another round trip. Page readers ask for pages one by one, and
 * the pages a tile needs from one column are usually short runs with gaps.
 */
export const coalescing = (
  source: RangeSource,
  {
    maxGap = 32 * 1024,
    minGap = 4 * 1024,
    gapRatio = 0.5,
    maxRun = 4 * 1024 * 1024,
    windowMs = 0,
  } = {},
): RangeSource => {
  let pending: Pending[] = []
  let scheduled = false

  const flush = () => {
    scheduled = false
    const batch = pending
      .filter((p) => {
        if (!p.signal?.aborted) return true
        p.reject(p.signal.reason)
        return false
      })
      .sort((a, b) => a.start - b.start)
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
      const cleanups: (() => void)[] = []
      for (const m of live) {
        const abort = () => {
          live.delete(m)
          m.reject(m.signal!.reason)
          if (!live.size && members.every((x) => x.signal)) controller.abort()
        }
        m.signal!.addEventListener('abort', abort, { once: true })
        cleanups.push(() => m.signal!.removeEventListener('abort', abort))
      }
      source
        .slice(start, end, members.every((m) => m.signal) ? controller.signal : undefined)
        .then(
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
        .finally(() => cleanups.forEach((cleanup) => cleanup()))
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
          if (windowMs > 0) setTimeout(flush, windowMs)
          else queueMicrotask(flush)
        }
      })
    },
  }
}
