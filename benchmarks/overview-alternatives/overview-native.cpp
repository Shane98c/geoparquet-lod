// Experimental native scan of production MultiPolygon packed own geometry.
// Hosted copies are intentionally omitted; source-aligned own values are exact.
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>

struct Cursor {
  const uint8_t *p, *end;
  uint64_t read() {
    uint64_t value = 0;
    for (int shift = 0; shift < 70; shift += 7) {
      if (p == end) throw std::runtime_error("truncated varint");
      const uint8_t byte = *p++;
      if (shift == 63 && (byte & 0x7e)) throw std::runtime_error("varint overflow");
      value |= uint64_t(byte & 127) << shift;
      if (!(byte & 128)) return value;
    }
    throw std::runtime_error("varint overflow");
  }
  int64_t delta() { const uint64_t v = read(); return int64_t(v >> 1) ^ -int64_t(v & 1); }
};

extern "C" int inspect_own(const int32_t *offsets, const uint8_t *bytes,
    const uint8_t *valid, int64_t valid_offset, int64_t rows, uint64_t *totals,
    uint8_t *present, int32_t *boxes, uint64_t *height_bits,
    int32_t *class_codes, uint8_t *attribute_presence, int64_t *error_row) {
  totals[0] = totals[1] = totals[2] = 0;
  for (int64_t row = 0; row < rows; ++row) {
    try {
      present[row] = 0; attribute_presence[row] = 0;
      height_bits[row] = 0; class_codes[row] = -1;
      if (valid && !(valid[(row + valid_offset) / 8] & (1 << ((row + valid_offset) % 8)))) continue;
      Cursor c{bytes + offsets[row], bytes + offsets[row + 1]};
      const uint64_t polygons = c.read();
      if (!polygons) continue;
      present[row] = 1; totals[0] += polygons;
      int32_t xmin = INT32_MAX, ymin = INT32_MAX, xmax = INT32_MIN, ymax = INT32_MIN;
      for (uint64_t p = 0; p < polygons; ++p) {
        const uint64_t rings = c.read(); totals[1] += rings;
        for (uint64_t ring = 0; ring < rings; ++ring) {
          const uint64_t vertices = c.read(); totals[2] += vertices;
          int64_t x = 0, y = 0;
          for (uint64_t v = 0; v < vertices; ++v) {
            x += c.delta(); y += c.delta();
            if (x < INT32_MIN || x > INT32_MAX || y < INT32_MIN || y > INT32_MAX)
              throw std::runtime_error("coordinate overflow");
            if (x < xmin) xmin = int32_t(x); if (x > xmax) xmax = int32_t(x);
            if (y < ymin) ymin = int32_t(y); if (y > ymax) ymax = int32_t(y);
          }
        }
      }
      boxes[row*4] = xmin; boxes[row*4+1] = ymin;
      boxes[row*4+2] = xmax; boxes[row*4+3] = ymax;
      const uint64_t mask = c.read();
      if (mask > 3) throw std::runtime_error("unexpected attribute layout");
      attribute_presence[row] = uint8_t(mask);
      if (mask & 1) height_bits[row] = c.read();
      if (mask & 2) class_codes[row] = int32_t(c.read());
    } catch (...) { *error_row = row; return 1; }
  }
  return 0;
}

extern "C" int fill_own(const int32_t *offsets, const uint8_t *bytes,
    const uint8_t *valid, int64_t valid_offset, int64_t rows,
    int32_t *geometry_offsets, int32_t *polygon_offsets, int32_t *ring_offsets,
    int32_t *xs, int32_t *ys, int64_t *error_row) {
  int64_t polygons = 0, rings = 0, vertices = 0;
  geometry_offsets[0] = polygon_offsets[0] = ring_offsets[0] = 0;
  for (int64_t row = 0; row < rows; ++row) {
    try {
      if (!valid || valid[(row + valid_offset) / 8] & (1 << ((row + valid_offset) % 8))) {
        Cursor c{bytes + offsets[row], bytes + offsets[row + 1]};
        const uint64_t count = c.read();
        for (uint64_t p = 0; p < count; ++p) {
          const uint64_t count_rings = c.read();
          for (uint64_t r = 0; r < count_rings; ++r) {
            const uint64_t count_vertices = c.read(); int64_t x = 0, y = 0;
            for (uint64_t v = 0; v < count_vertices; ++v) {
              x += c.delta(); y += c.delta(); xs[vertices] = int32_t(x); ys[vertices] = int32_t(y); ++vertices;
            }
            ring_offsets[++rings] = int32_t(vertices);
          }
          polygon_offsets[++polygons] = int32_t(rings);
        }
      }
      geometry_offsets[row + 1] = int32_t(polygons);
    } catch (...) { *error_row = row; return 1; }
  }
  return 0;
}
