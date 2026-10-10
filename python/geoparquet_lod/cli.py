"""geoparquet-lod: write, validate and inspect LOD column files."""

from __future__ import annotations

import json

import click

from . import thinning as th
from .validate import ValidationError, inspect, validate
from .writer import write


def parse_thinning(spec: str, where: str | None) -> th.Thin:
    """`drop[:rate]`, `density[:budget]`, `size[:pixels]`, `importance:column[:per_tile]`,
    `cluster[:pixels]`; `+` combines, e.g. `size:4+drop:2.5`. `where` restricts eligibility."""
    parts = []
    for part in spec.split('+'):
        kind, _, rest = part.partition(':')
        if kind == 'drop':
            parts.append(th.drop_rate(float(rest or 2.5)))
        elif kind == 'density':
            parts.append(th.density_budget(int(rest or 60_000)))
        elif kind == 'size':
            parts.append(th.size_threshold(float(rest or 4)))
        elif kind == 'importance':
            column, _, per_tile = rest.partition(':')
            parts.append(th.importance(column, int(per_tile or 1000)))
        elif kind == 'cluster':
            parts.append(th.cluster(float(rest or 8)))
        else:
            raise click.BadParameter(f'unknown thinning {part!r}')
    thin = parts[0] if len(parts) == 1 else th.combine(*parts)
    return th.only(where, thin) if where else thin


@click.group()
def cli():
    """Write GeoParquet files that a web map can read at tile cost."""


@cli.command('write')
@click.argument('src')
@click.argument('out')
@click.option(
    '--thinning',
    'thinning_spec',
    default='drop:2.5',
    show_default=True,
    help='drop[:rate] | density[:budget] | size[:pixels] | importance:column[:n] | cluster[:px]; join with +',
)
@click.option(
    '--where', default=None, help='SQL predicate; rows failing it are never shown zoomed out'
)
@click.option('--columns', multiple=True, help='extra source columns the thinning reads')
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
    where,
    columns,
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
        thinning=parse_thinning(thinning_spec, where),
        columns=columns,
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
