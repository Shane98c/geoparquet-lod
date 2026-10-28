import type { Compressors } from 'hyparquet'
import { compressors as base } from 'hyparquet-compressors'
import { ZSTDDecoder } from 'zstddec'

const WASM_WAIT_MS = 1000
const wasm = new ZSTDDecoder()
let wasmLoaded = false

/**
 * Settles once WASM zstd has loaded or failed to, or after `WASM_WAIT_MS`.
 * zstd pages decode in JS until it loads.
 */
export const zstdReady = new Promise<void>((resolve) => {
  const timer = setTimeout(resolve, WASM_WAIT_MS)
  wasm
    .init()
    .then(() => {
      wasmLoaded = true
    })
    .catch(() => {})
    .finally(() => {
      clearTimeout(timer)
      resolve()
    })
})

export const compressors: Compressors = {
  ...base,
  ZSTD(input, outputLength) {
    // zstddec does not report zstd errors, so only an output of the expected,
    // non-zero length is trusted; anything else goes to fzstd, which throws
    if (wasmLoaded && outputLength > 0) {
      const output = wasm.decode(input, outputLength)
      if (output.length === outputLength) return output
    }
    return base.ZSTD!(input, outputLength)
  },
}
