import type { DecodedTile, TileOptions } from './tile.js'
import type { PreparedTile } from './spatial.js'
import type { ProjectionTask } from './projection.js'

export type TileWork =
  | { type: 'render'; task: PreparedTile; options: Omit<TileOptions, 'signal'> }
  | { type: 'decode'; task: PreparedTile; options?: Omit<TileOptions, 'signal'> }
  | { type: 'encode'; tile: DecodedTile; options: Omit<TileOptions, 'signal'> }
  | { type: 'project'; task: ProjectionTask }
export type TileJob = TileWork & { id: number }
export type TileResult =
  { data: ArrayBuffer } | { tile: DecodedTile; data?: ArrayBuffer } | { values: unknown[] }
export type TileReply =
  { ready: true } | ({ id: number } & TileResult) | { id: number; error: string; name: string }
export type CpuRequest = TileJob | { type: 'cpu-init' }
