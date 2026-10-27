/** LOD coordinates are EPSG:3857 metres; source geometry is longitude/latitude. */
export const LOD_CRS: 'EPSG:3857' = 'EPSG:3857'
export const EARTH_RADIUS = 6378137
export const HALF_WORLD = Math.PI * EARTH_RADIUS
export const MAX_LATITUDE = 85.0511287798066

export const mercatorX = (longitude: number) => (EARTH_RADIUS * longitude * Math.PI) / 180

export const mercatorY = (latitude: number) => {
  const clamped = Math.max(-MAX_LATITUDE, Math.min(MAX_LATITUDE, latitude))
  return EARTH_RADIUS * Math.log(Math.tan(Math.PI / 4 + (clamped * Math.PI) / 360))
}

export const longitudeOf = (x: number) => (x / EARTH_RADIUS) * (180 / Math.PI)

export const latitudeOf = (y: number) => Math.atan(Math.sinh(y / EARTH_RADIUS)) * (180 / Math.PI)

/** Whether a geometry column's `offset + scale * value` is EPSG:3857 rather than lon/lat. */
export const isMercator = (column: object) => (column as { crs?: string }).crs === LOD_CRS
