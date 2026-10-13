declare module 'lineclip' {
  type Position = [number, number]
  type Bbox = [number, number, number, number]
  export function clipPolygon(points: Position[], bbox: Bbox): Position[]
  export function clipPolyline(points: Position[], bbox: Bbox): Position[][]
}
