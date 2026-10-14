// Experimental compact-index reader. Deliberately separate from the public reader.
import { parquetMetadata } from '../../node_modules/hyparquet/src/metadata.js'
import { getSchemaPath } from '../../node_modules/hyparquet/src/schema.js'
import { readColumn } from '../../node_modules/hyparquet/src/column.js'
import { assembleNested } from '../../node_modules/hyparquet/src/assemble.js'
import { DEFAULT_PARSERS } from '../../node_modules/hyparquet/src/convert.js'
import { compressors } from 'hyparquet-compressors'
import { GeometryBuilder } from '../../src/geometry.js'
import { attributeKind, decodeSelectedRow } from '../../src/decode.js'
import { tileBounds } from '../../src/tile.js'
import { ByteCache } from './byte-cache.js'
import { directCoordinates } from './direct-coordinates.js'
import { toMvt } from '../../src/mvt.js'

type Box = [number, number, number, number]
type Page = [number, number, Record<string, Box | null>, Record<string, [number, number]>]
type Shard = {
  kind: string
  first: number
  last: number
  bbox: Box | null
  range: [number, number]
}
const overlaps = (a: ArrayLike<number>, b: ArrayLike<number>) =>
  a[0] <= b[2] && a[2] >= b[0] && a[1] <= b[3] && a[3] >= b[1]
const gunzip = async (bytes: ArrayBuffer) =>
  JSON.parse(
    await new Response(
      new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip')),
    ).text(),
  )

class RangeCache {
  requests = 0
  bytes = 0
  trace: number[][] = []
  cache = new Map<string, Promise<ArrayBuffer>>()
  sizes = new Map<string, number>()
  total = 0
  pending: {
    start: number
    size: number
    resolve: (b: ArrayBuffer) => void
    reject: (e: unknown) => void
  }[] = []
  blocked: [number, number][] = []
  maxGap = 8192
  active = 0
  waiters: (() => void)[] = []
  timer: ReturnType<typeof setTimeout> | null = null
  constructor(
    readonly url: string,
    readonly read: (start: number, size: number) => Promise<ArrayBuffer>,
  ) {}
  get(start: number, size: number): Promise<ArrayBuffer> {
    const key = `${start}:${size}`
    const cached = this.cache.get(key)
    if (cached) {
      this.cache.delete(key)
      this.cache.set(key, cached)
      return cached
    }
    const result = new Promise<ArrayBuffer>((resolve, reject) => {
      this.pending.push({ start, size, resolve, reject })
      this.timer ??= setTimeout(() => {
        this.timer = null
        void this.flush()
      }, 1)
    })
    this.cache.set(key, result)
    this.sizes.set(key, size)
    this.total += size
    while (this.total > 32 * 1024 * 1024 && this.cache.size > 1) {
      const oldest = this.cache.keys().next().value!
      this.total -= this.sizes.get(oldest)!
      this.sizes.delete(oldest)
      this.cache.delete(oldest)
    }
    return result
  }
  async flush() {
    const pending = this.pending.splice(0).sort((a, b) => a.start - b.start)
    const runs: (typeof pending)[] = []
    for (const item of pending) {
      const last = runs.at(-1),
        end = last?.at(-1)
      const gap = end ? item.start - end.start - end.size : Infinity
      const bytes = last?.reduce((n, p) => n + p.size, 0) ?? 0
      const priorGaps = last ? end!.start + end!.size - last[0].start - bytes : 0
      const forbidden =
        end &&
        this.blocked.some(
          ([offset, length]) => offset < item.start && offset + length > end.start + end.size,
        )
      if (
        end &&
        gap >= 0 &&
        gap <= this.maxGap &&
        priorGaps + gap <= (bytes + item.size) * 0.25 &&
        item.start + item.size - last![0].start <= 4 * 1024 * 1024 &&
        !forbidden
      )
        last!.push(item)
      else runs.push([item])
    }
    await Promise.all(
      runs.map(async (run) => {
        const first = run[0].start,
          end = run.at(-1)!.start + run.at(-1)!.size
        // National coarse styling can touch thousands of sparse source pages.
        // Bound browser fetches across all concurrent tile/flush calls.
        if (this.active >= 8) await new Promise<void>((resolve) => this.waiters.push(resolve))
        else this.active++
        try {
          this.requests++
          this.bytes += end - first
          this.trace.push([first, end - first])
          const data = await this.read(first, end - first)
          if (data.byteLength !== end - first) throw Error('Truncated range response')
          for (const item of run)
            item.resolve(data.slice(item.start - first, item.start - first + item.size))
        } catch (error) {
          for (const item of run) item.reject(error)
        } finally {
          const next = this.waiters.shift()
          if (next) next()
          else this.active--
        }
      }),
    )
  }
}

export class ExperimentalReader {
  root: any
  pages: Page[] = []
  schema: any
  fields: any[] = []
  attributePages: number[] = []
  shards = new Map<number, Promise<void>>()
  range: RangeCache
  geometryCache = new ByteCache<any>(32 * 1024 * 1024)
  leafCache = new ByteCache<any>(8 * 1024 * 1024)
  decoded = new Map<string, Promise<any>>()
  metrics = { decodeMs: 0, mvtMs: 0, tileMs: 0, tiles: 0, candidates: 0 }
  constructor(
    readonly url: string,
    read?: (start: number, size: number) => Promise<ArrayBuffer>,
    readonly options = { directCoordinates: true },
  ) {
    this.range = new RangeCache(
      url,
      read ??
        (async (start, size) => {
          const response = await fetch(url, {
            headers: { Range: `bytes=${start}-${start + size - 1}` },
            cache: 'no-store',
          })
          if (response.status !== 206) throw Error(`Expected 206, received ${response.status}`)
          return response.arrayBuffer()
        }),
    )
  }
  async open() {
    const boot = await this.range.get(4, 16384),
      view = new DataView(boot)
    if (new TextDecoder().decode(new Uint8Array(boot, 0, 8)) !== 'NESTIDX1')
      throw Error('Bad experiment magic')
    this.root = await gunzip(boot.slice(12, 12 + view.getUint32(8, true)))
    if (this.root.shard_manifest)
      this.root.shards = await gunzip(
        await this.range.get(...(this.root.shard_manifest as [number, number])),
      )
    if (this.root.shards) {
      if (this.root.layout !== 'compact-rows') this.range.maxGap = 0
    } else
      this.pages = await gunzip(await this.range.get(...(this.root.directory as [number, number])))
    this.range.blocked =
      this.root.blocked ??
      this.pages.flatMap((p) => ['id', 'geometry'].map((name) => p[3][name]).filter(Boolean))
    this.attributePages = this.pages.flatMap((p, i) => (p[3].height && p[3].class ? [i] : []))
    const schemaBytes = Uint8Array.from(atob(this.root.schema), (c) => c.charCodeAt(0)).buffer
    this.schema = parquetMetadata(schemaBytes, { geoparquet: false }).schema
    this.fields = this.root.attributes.map((a: any) => ({
      name: a.name,
      kind: a.values ? 'enum' : attributeKind(a.type),
      values: a.values,
    }))
  }
  async loadShards(matches: (shard: Shard) => boolean) {
    await Promise.all(
      (this.root.shards ?? []).flatMap((shard: Shard, index: number) => {
        if (!matches(shard)) return []
        let work = this.shards.get(index)
        if (!work) {
          work = (async () => {
            const pages: Page[] = await gunzip(await this.range.get(...shard.range))
            for (const page of pages) {
              const id = this.pages.length
              this.pages.push(page)
              if (page[3].height && page[3].class) this.attributePages.push(id)
            }
            this.attributePages.sort((a, b) => this.pages[a][0] - this.pages[b][0])
          })()
          this.shards.set(index, work)
        }
        return [work]
      }),
    )
  }
  attributePage(id: number) {
    let low = 0,
      high = this.attributePages.length
    while (low < high) {
      const mid = (low + high) >>> 1,
        page = this.pages[this.attributePages[mid]]
      if (page[1] <= id) low = mid + 1
      else high = mid
    }
    const page = this.attributePages[low]
    return page !== undefined && this.pages[page][0] <= id ? page : undefined
  }
  remember(key: string, build: () => Promise<any>) {
    if (this.root.layout === 'compact-rows')
      return (key.startsWith('geometry:') ? this.geometryCache : this.leafCache).get(key, build)
    const prior = this.decoded.get(key)
    if (prior) {
      this.decoded.delete(key)
      this.decoded.set(key, prior)
      return prior
    }
    const value = build()
    this.decoded.set(key, value)
    // Entry limit, not a claimed heap-byte bound. Separate compressed cache is 32 MiB.
    while (this.decoded.size > 512) this.decoded.delete(this.decoded.keys().next().value!)
    return value
  }
  async leaf(page: number, name: string) {
    return this.remember(`leaf:${page}:${name}`, async () => {
      const p = this.pages[page]
      const level = name.split('.')[0]
      if (level in this.root.lods && this.root.layout !== 'independent-pages') {
        const geometryRanges = Object.entries(p[3]).filter(
          ([leaf]) => leaf === level || leaf.startsWith(`${level}.`),
        )
        const geometryBytes = geometryRanges.reduce((n, [, [, length]]) => n + length, 0)
        const start = p[3].height[0],
          end = Math.max(...geometryRanges.map(([, [offset, length]]) => offset + length))
        const overhead = end - start - geometryBytes
        // Same bounded corridor policy for both encodings; selected spatial page only.
        // Cache incidental scalar/overview chunks so later style changes can reuse bytes.
        if (overhead >= 0 && overhead <= 8192 && overhead <= geometryBytes * 0.25) {
          for (const [leaf, [offset, length]] of Object.entries(p[3]))
            if (leaf !== 'id' && leaf !== 'geometry' && offset >= start && offset + length <= end)
              void this.range.get(offset, length).catch(() => undefined)
        }
      }
      const buffer = await this.range.get(...p[3][name])
      const start = performance.now(),
        path = getSchemaPath(this.schema, name.split('.'))
      const element = path.at(-1)!.element
      const { data } = readColumn(
        { view: new DataView(buffer), offset: 0 },
        { groupStart: p[0], selectStart: 0, selectEnd: p[1] - p[0] },
        {
          type: element.type,
          codec: 'ZSTD',
          element,
          schemaPath: path,
          pathInSchema: name.split('.'),
          compressors,
          parsers: DEFAULT_PARSERS,
          utf8: element.converted_type === 'UTF8',
        },
      )
      this.metrics.decodeMs += performance.now() - start
      return data.flatMap((chunk: any) => Array.from(chunk))
    })
  }
  async geometry(page: number, level: string) {
    return this.remember(`geometry:${page}:${level}`, async () => {
      const p = this.pages[page],
        lod = this.root.lods[level]
      let builder = new GeometryBuilder()
      const ids: number[] = [],
        attrs: unknown[][] = []
      if (this.root.variant === 'packed') {
        const values = await this.leaf(page, level),
          start = performance.now()
        values.forEach((blob: Uint8Array | null, local: number) => {
          if (!blob) return
          decodeSelectedRow(
            blob,
            'MultiPolygon',
            this.fields,
            builder,
            true,
            p[0] + local,
            -1,
            (id, values) => {
              ids.push(id)
              attrs.push(values)
            },
          )
        })
        this.metrics.decodeMs += performance.now() - start
      } else {
        const compact = this.root.layout === 'compact-rows'
        const geometryName = compact ? 'geometry' : level
        const names = Object.keys(p[3]).filter((name) => name.startsWith(`${geometryName}.`))
        const direct = compact && this.options.directCoordinates
        const [leaves, sourceIds, carried] = await Promise.all([
          direct
            ? Promise.all(names.map((name) => this.range.get(...p[3][name])))
            : Promise.all(names.map((name) => this.leaf(page, name))),
          compact ? this.leaf(page, 'source_row') : Promise.resolve(null),
          compact
            ? Promise.all(this.fields.map((field) => this.leaf(page, field.name)))
            : Promise.resolve([]),
        ])
        let start = performance.now()
        const xIndex = names.findIndex((name) => name.endsWith('.x'))
        const yIndex = names.findIndex((name) => name.endsWith('.y'))
        const fast =
          direct && xIndex >= 0 && yIndex >= 0
            ? directCoordinates(
                leaves[xIndex],
                leaves[yIndex],
                getSchemaPath(this.schema, names[xIndex].split('.')),
                getSchemaPath(this.schema, names[yIndex].split('.')),
                p[1] - p[0],
              )
            : undefined
        if (fast) {
          builder = fast
          for (let local = 0; local < fast.length; local++) {
            ids.push(Number(sourceIds[local]))
            attrs.push(carried.map((column) => column[local]))
          }
          this.metrics.decodeMs += performance.now() - start
        } else {
          // Reuse fetched compressed bytes on schemas/pages outside the fast path.
          this.metrics.decodeMs += performance.now() - start
          const ordinary = direct
            ? await Promise.all(names.map((name) => this.leaf(page, name)))
            : leaves
          start = performance.now()
          const columns = new Map(names.map((name, i) => [name, ordinary[i]]))
          assembleNested(columns, getSchemaPath(this.schema, [geometryName])[1], DEFAULT_PARSERS)
          const rows = columns.get(geometryName)!
          rows.forEach((polygons: any, local: number) => {
            if (!polygons?.length) return
            ids.push(sourceIds ? Number(sourceIds[local]) : p[0] + local)
            if (compact) attrs.push(carried.map((column) => column[local]))
            builder.begin('MultiPolygon')
            for (const polygon of polygons) {
              builder.startPolygon()
              for (const ring of polygon) {
                builder.startRing()
                const offset = builder.reserveCoordinates(ring.length)
                for (let k = 0; k < ring.length; k++) {
                  builder.x[offset + k] = ring[k].x
                  builder.y[offset + k] = ring[k].y
                }
              }
            }
          })
          this.metrics.decodeMs += performance.now() - start
        }
      }
      const geometry = builder.finish(lod.scale, lod.offset),
        boxes = new Float64Array(ids.length * 4)
      for (let row = 0; row < ids.length; row++) {
        let xmin = Infinity,
          ymin = Infinity,
          xmax = -Infinity,
          ymax = -Infinity
        for (
          let poly = geometry.geometryOffsets[row];
          poly < geometry.geometryOffsets[row + 1];
          poly++
        )
          for (
            let ring = geometry.polygonOffsets[poly];
            ring < geometry.polygonOffsets[poly + 1];
            ring++
          )
            for (let c = geometry.ringOffsets[ring]; c < geometry.ringOffsets[ring + 1]; c++) {
              const x = geometry.x[c] * lod.scale[0] + lod.offset[0],
                y = geometry.y[c] * lod.scale[1] + lod.offset[1]
              xmin = Math.min(xmin, x)
              ymin = Math.min(ymin, y)
              xmax = Math.max(xmax, x)
              ymax = Math.max(ymax, y)
            }
        boxes.set([xmin, ymin, xmax, ymax], row * 4)
      }
      return { ids, attrs, geometry, boxes }
    })
  }
  async render(z: number, x: number, y: number, properties: string[] = []) {
    const begin = performance.now(),
      box = tileBounds(z, x, y)
    const levels = Object.keys(this.root.lods).sort(
      (a, b) => this.root.lods[a].zooms[0] - this.root.lods[b].zooms[0],
    )
    const level =
      levels.find((name) => Math.max(...this.root.lods[name].zooms) >= z) ?? levels.at(-1)!
    await this.loadShards(
      (shard) => shard.kind === level && !!shard.bbox && overlaps(shard.bbox, box),
    )
    const selected = this.pages
      .flatMap((p, i) => (p[2][level] && overlaps(p[2][level]!, box) ? [i] : []))
      .sort((a, b) => this.pages[a][0] - this.pages[b][0])
    this.metrics.candidates += selected.length
    const pageData = await Promise.all(
      selected.map(async (page) => {
        // Geometry and ordinary source attributes can share adjacent physical ranges.
        const [geometry, values] = await Promise.all([
          this.geometry(page, level),
          this.root.variant === 'nested' &&
          !['independent-pages', 'compact-rows'].includes(this.root.layout)
            ? Promise.all(properties.map((name) => this.leaf(page, name)))
            : Promise.resolve([]),
        ])
        return { page, ...geometry, values }
      }),
    )
    // Independently paged source attributes are fetched only for visible source rows,
    // after filtering geometry. Source values remain stored once in normal columns.
    const attributes = new Map<number, unknown[]>()
    if (
      this.root.variant === 'nested' &&
      this.root.layout === 'independent-pages' &&
      properties.length
    ) {
      const visibleIds: number[] = []
      for (const data of pageData)
        for (let row = 0; row < data.ids.length; row++)
          if (overlaps(data.boxes.subarray(row * 4, row * 4 + 4), box))
            visibleIds.push(data.ids[row])
      visibleIds.sort((a, b) => a - b)
      await this.loadShards((shard) => {
        if (shard.kind !== 'attributes') return false
        let low = 0,
          high = visibleIds.length
        while (low < high) {
          const mid = (low + high) >>> 1
          if (visibleIds[mid] < shard.first) low = mid + 1
          else high = mid
        }
        return low < visibleIds.length && visibleIds[low] < shard.last
      })
      const wanted = new Map<number, number[]>()
      for (const id of visibleIds) {
        const page = this.attributePage(id)
        if (page === undefined) throw Error(`Missing attribute page for source row ${id}`)
        const ids = wanted.get(page) ?? []
        ids.push(id)
        wanted.set(page, ids)
      }
      await Promise.all(
        [...wanted].map(async ([page, ids]) => {
          const values = await Promise.all(properties.map((name) => this.leaf(page, name)))
          // Retain requested scalars here, not whole source pages for a sparse overview.
          for (const id of ids)
            attributes.set(
              id,
              values.map((column) => column[id - this.pages[page][0]]),
            )
        }),
      )
    }
    const builder = new GeometryBuilder(),
      rows: number[] = [],
      columns: Record<string, unknown[]> = Object.fromEntries(properties.map((p) => [p, []]))
    const lod = this.root.lods[level]
    for (const data of pageData)
      for (let row = 0; row < data.ids.length; row++) {
        if (!overlaps(data.boxes.subarray(row * 4, row * 4 + 4), box)) continue
        const id = data.ids[row],
          g = data.geometry
        rows.push(id)
        builder.begin('MultiPolygon')
        for (let poly = g.geometryOffsets[row]; poly < g.geometryOffsets[row + 1]; poly++) {
          builder.startPolygon()
          for (let ring = g.polygonOffsets[poly]; ring < g.polygonOffsets[poly + 1]; ring++) {
            builder.startRing()
            const a = g.ringOffsets[ring],
              b = g.ringOffsets[ring + 1],
              at = builder.reserveCoordinates(b - a)
            builder.x.set(g.x.subarray(a, b), at)
            builder.y.set(g.y.subarray(a, b), at)
          }
        }
        properties.forEach((name, i) => {
          const attr = attributes.get(id)
          columns[name].push(
            this.root.variant === 'packed' || this.root.layout === 'compact-rows'
              ? data.attrs[row][this.fields.findIndex((f) => f.name === name)]
              : attr
                ? attr[i]
                : data.values[i][id - this.pages[data.page][0]],
          )
        })
      }
    const mvtStart = performance.now()
    const data = toMvt(
      { geometry: builder.finish(lod.scale, lod.offset) as never, rowIndex: rows, columns },
      { z, x, y, layer: 'features' },
    )
    const mvtMs = performance.now() - mvtStart
    this.metrics.mvtMs += mvtMs
    this.metrics.tileMs += performance.now() - begin
    this.metrics.tiles++
    return { data, rows, mvtMs, ms: performance.now() - begin, level }
  }
  stats() {
    return {
      requests: this.range.requests,
      bytes: this.range.bytes,
      trace: this.range.trace,
      directoryShards: this.shards.size,
      indexedPages: this.pages.length,
      compactReader: this.root.layout === 'compact-rows' ? 'byte-cache-direct-v1' : undefined,
      geometryCacheBytes: this.geometryCache.bytes,
      leafCacheBytes: this.leafCache.bytes,
      geometryCacheEntries: this.geometryCache.values.size,
      compressedCacheBytes: this.range.total,
      decodedEntries:
        this.decoded.size + this.geometryCache.values.size + this.leafCache.values.size,
      ...this.metrics,
    }
  }
}
