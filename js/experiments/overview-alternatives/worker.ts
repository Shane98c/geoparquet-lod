import { ExperimentalReader } from './reader.js'
let reader: ExperimentalReader
self.onmessage = async ({ data: message }) => {
  const { id, type } = message
  try {
    let result: unknown
    if (type === 'open') {
      reader = new ExperimentalReader(message.url)
      await reader.open()
      result = reader.stats()
    } else if (type === 'stats') result = reader.stats()
    else if (type === 'tile')
      result = await reader.render(message.z, message.x, message.y, message.properties)
    else throw Error('Unknown operation')
    const transfer = type === 'tile' ? [(result as any).data] : []
    self.postMessage({ id, result }, { transfer })
  } catch (error) {
    self.postMessage({ id, error: String(error), stack: (error as Error).stack })
  }
}
