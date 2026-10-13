import type { TileOptions } from './tile.js'
import type { PreparedTile } from './spatial.js'

export interface TileJob {
  type: 'render'
  id: number
  task: PreparedTile
  options: Omit<TileOptions, 'signal'>
}
export type TileReply =
  { ready: true } | { id: number; data: ArrayBuffer } | { id: number; error: string; name: string }

export type CpuRequest = TileJob | { type: 'cpu-init' }
