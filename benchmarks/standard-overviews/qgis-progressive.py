"""Progressive-render benchmark: painted extent over time while QGIS renders a GeoParquet file.

Polls QgsMapRendererParallelJob.renderedImage() while the job runs, the same
partial image the canvas shows as a preview, and records the fraction of the
final painted pixels present at each sample. Run with the QGIS bundle's Python:

    A=/Applications/QGIS-final-4_2_1.app/Contents
    PROJ_LIB=$A/Resources/qgis/proj PYTHONHOME=$A/Frameworks QT_QPA_PLATFORM=offscreen \
      $A/MacOS/python3.12 benchmarks/standard-overviews/qgis-progressive.py
"""

import json
import time

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFillSymbol,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication, QSize
from qgis.PyQt.QtGui import QColor

A = '/Applications/QGIS-final-4_2_1.app/Contents'

QgsApplication.setPrefixPath(A + '/MacOS', True)
app = QgsApplication([], False)
app.initQgis()

FILES = {
    'single': '/private/tmp/std-overviews/tokyo.packed.parquet',
    'prefix': '/private/tmp/std-overviews/tokyo.prefix.parquet',
}
EXTENTS = {
    'full': QgsRectangle(139.39, 35.45, 140.10, 35.90),
    'city': QgsRectangle(139.65, 35.62, 139.87, 35.74),
}
WIDTH, HEIGHT = 1280, 800


def painted(image):
    image = image.convertToFormat(image.Format.Format_ARGB32)
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    import numpy as np

    a = np.frombuffer(ptr, np.uint8).reshape(image.height(), image.width(), 4)
    return int((a[:, :, 3] > 0).sum())


def render(path, extent):
    layer = QgsVectorLayer(path, 'buildings', 'ogr')
    assert layer.isValid(), path
    symbol = QgsFillSymbol.createSimple({'color': '52,185,163,255', 'outline_style': 'no'})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:4326'))
    settings.setExtent(extent)
    settings.setOutputSize(QSize(WIDTH, HEIGHT))
    settings.setBackgroundColor(QColor(0, 0, 0, 0))
    job = QgsMapRendererParallelJob(settings)
    samples = []
    t0 = time.perf_counter()
    job.start()
    while job.isActive():
        QCoreApplication.processEvents()
        time.sleep(0.1)
        samples.append((round(time.perf_counter() - t0, 2), painted(job.renderedImage())))
    total = time.perf_counter() - t0
    final = painted(job.renderedImage())
    return {
        'seconds': round(total, 2),
        'final_pixels': final,
        'samples': [(t, round(p / max(final, 1), 3)) for t, p in samples],
    }


out = {}
for run in range(3):
    for name, path in list(FILES.items()) if run % 2 == 0 else list(FILES.items())[::-1]:
        for ext_name, extent in EXTENTS.items():
            r = render(path, extent)
            out.setdefault(f'{name}/{ext_name}', []).append(r)
            print(
                name,
                ext_name,
                'run',
                run,
                r['seconds'],
                's; coverage at 1s/2s/5s:',
                [next((c for t, c in r['samples'] if t >= s), 1.0) for s in (1, 2, 5)],
                flush=True,
            )
json.dump(out, open('/private/tmp/std-overviews/qgis_progressive.json', 'w'), indent=1)
app.exitQgis()
