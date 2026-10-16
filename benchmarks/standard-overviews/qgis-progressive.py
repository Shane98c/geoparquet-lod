"""Progressive-render benchmark: painted extent over time while QGIS renders a GeoParquet file.

Polls QgsMapRendererParallelJob.renderedImage() while the job runs, the same
partial image the canvas shows as a preview, and records the fraction of the
final painted pixels present at each sample. Needs QGIS's bundled Python, not
the project environment. On macOS:

    export QGIS_CONTENTS=/Applications/QGIS.app/Contents
    PROJ_LIB=$QGIS_CONTENTS/Resources/qgis/proj PYTHONHOME=$QGIS_CONTENTS/Frameworks \
      QT_QPA_PLATFORM=offscreen $QGIS_CONTENTS/MacOS/python3.12 \
      benchmarks/standard-overviews/qgis-progressive.py tokyo.packed.parquet tokyo.prefix.parquet out.json

write-tokyo.py builds the two files. The summary holds the median over runs of
the render time and of the coverage at each listed second.
"""

import json
import os
import statistics
import sys
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

QgsApplication.setPrefixPath(os.environ['QGIS_CONTENTS'] + '/MacOS', True)
app = QgsApplication([], False)
app.initQgis()

FILES = {'single': sys.argv[1], 'prefix': sys.argv[2]}
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


def coverage_at(result, second):
    return next((c for t, c in result['samples'] if t >= second), 1.0)


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
                [coverage_at(r, s) for s in (1, 2, 5)],
                flush=True,
            )


summary = {
    key: {
        'seconds': round(statistics.median(r['seconds'] for r in results), 2),
        'coverage': {
            f'{s:g}': round(statistics.median(coverage_at(r, s) for r in results), 3)
            for s in (0.5, 1, 2, 5, 10)
        },
    }
    for key, results in out.items()
}
json.dump({'summary': summary, 'runs': out}, open(sys.argv[3], 'w'), indent=1)
app.exitQgis()
