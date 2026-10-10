"""geoparquet-lod: write, validate and inspect LOD column files."""

from __future__ import annotations

import json

import click

from . import thinning as th
from .validate import ValidationError, inspect, validate
from .writer import write


def parse_thinning(spec: str) -> th.Thinning:
    """`density:60000`, `density:60000:risk > 0`, `size:4`, `importance:population:500`."""
    kind, _, rest = spec.partition(':')
    if kind == 'density':
        budget, _, where = rest.partition(':')
        return th.density_budget(int(budget or 60_000), where or None)
    if kind == 'size':
        return th.size_threshold(float(rest or 4))
    if kind == 'importance':
        order_by, _, per_tile = rest.partition(':')
        return th.importance(order_by, int(per_tile or 1000))
    raise click.BadParameter(f'unknown thinning {spec!r}; use density, size or importance')


@click.group()
def cli():
    """Write GeoParquet files that a web map can read at tile cost."""


@cli.command('write')
@click.argument('src')
@click.argument('out')
@click.option(
    '--thinning',
    'thinning_spec',
    default='density:60000',
    show_default=True,
    help='density[:budget[:where]] | size[:pixels] | importance:expr[:per_tile]',
)
@click.option('--zooms', default='2-16', show_default=True, help='inclusive range, e.g. 2-16')
@click.option(
    '--polygons-from',
    default=13,
    type=int,
    show_default=True,
    help='first zoom drawn as footprints; omit for points only',
)
@click.option('--points-only', is_flag=True, help='draw points at every zoom')
@click.option(
    '--carry', multiple=True, help='column[:type] to pack into the payload, e.g. risk:f32'
)
@click.option('--geometry', default='geometry', show_default=True)
@click.option('--prefix-zoom', default=6, type=int, show_default=True)
@click.option('--row-group-rows', default=1_048_576, type=int, show_default=True)
@click.option('--page-bytes', default=4096, type=int, show_default=True)
@click.option(
    '--batch-rows',
    default=None,
    type=int,
    help='rows per write batch; default sizes pages to --page-bytes',
)
@click.option('--workers', default=None, type=int)
@click.option('--memory-limit', default=None)
@click.option('--temp-dir', default=None)
def write_cmd(
    src,
    out,
    thinning_spec,
    zooms,
    polygons_from,
    points_only,
    carry,
    geometry,
    prefix_zoom,
    row_group_rows,
    page_bytes,
    batch_rows,
    workers,
    memory_limit,
    temp_dir,
):
    """Write SRC (GeoParquet with WKB geometry) to OUT in the LOD columns layout."""
    lo, _, hi = zooms.partition('-')
    carried = [tuple(c.split(':', 1)) if ':' in c else c for c in carry]
    write(
        src,
        out,
        thinning=parse_thinning(thinning_spec),
        zooms=range(int(lo), int(hi or lo) + 1),
        polygons_from=None if points_only else polygons_from,
        carry=carried,
        geometry=geometry,
        prefix_zoom=prefix_zoom,
        row_group_rows=row_group_rows,
        page_bytes=page_bytes,
        batch_rows=batch_rows,
        workers=workers,
        memory_limit=memory_limit,
        temp_dir=temp_dir,
        log=click.echo,
    )


@cli.command('validate')
@click.argument('path')
def validate_cmd(path):
    """Check the layout contract and decode a sample of every band."""
    try:
        summary = validate(path)
    except ValidationError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f'OK {path}: ' + json.dumps(summary))


@cli.command('inspect')
@click.argument('path')
def inspect_cmd(path):
    """Print the lod_columns metadata and per-column bytes of one row group."""
    info = inspect(path)
    rows = info['rows_in_sampled_group']
    click.echo(
        json.dumps({k: v for k, v in info.items() if k != 'compressed_bytes_by_column'}, indent=2)
    )
    click.echo(f'bytes per row in row group {info["row_group_sampled"]}:')
    for col, nbytes in info['compressed_bytes_by_column'].items():
        click.echo(f'  {col:40s} {nbytes / max(rows, 1):8.2f}')
