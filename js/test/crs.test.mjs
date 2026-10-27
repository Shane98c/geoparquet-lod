// the reader serves source geometry as longitude/latitude, so it refuses any other CRS
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { LodReader } from '../dist/index.js'

const [accepted, ...refused] = process.argv.slice(2)
if (!accepted) {
  console.log('crs test skipped: no fixture given')
  process.exit(0)
}
const open = async (path) => {
  const bytes = await readFile(path)
  return LodReader.open({
    byteLength: bytes.length,
    slice: async (a, b) => Uint8Array.from(bytes.subarray(a, b)).buffer,
  })
}
await open(accepted)
for (const path of refused) await assert.rejects(open(path), /OGC:CRS84|planar/, path)
console.log(`crs tests passed, ${refused.length} files refused`)
