"""geoparquet-lod: write, validate and inspect LOD column files."""

from __future__ import annotations

import json

import click

from . import thinning as th
from .validate import ValidationError, inspect, validate
from .writer import write


def parse_thinning(spec: str, where: str | None) -> th.Thin:
    """`drop[:rate]`, `density[:budget]`, `smallest[:bytes]`, `size[:pixels]`, `tiny[:pixels]`,
    `importance:column[:per_tile]`, `cluster[:pixels]`; `+` combines, e.g. `size:4+drop:2.5`;
    `>` chains, e.g. `smallest:600000>tiny`; `auto` is the family default. `where` restricts
    eligibility."""
    if spec == 'auto':
        thin = th.default_thinning('auto')
        return th.only(where, thin) if where else thin
    stages = [_parse_union(stage) for stage in spec.split('>')]
    thin = stages[0] if len(stages) == 1 else th.chain(*stages)
    return th.only(where, thin) if where else thin


def _parse_union(spec: str) -> th.Thin:
    parts = []
    for part in spec.split('+'):
        kind, _, rest = part.partition(':')
        if kind == 'drop':
            parts.append(th.drop_rate(float(rest or 2.5)))
        elif kind == 'density':
            parts.append(th.density_budget(int(rest or 60_000)))
        elif kind == 'smallest':
            parts.append(th.drop_smallest(int(rest or 600_000)))
        elif kind == 'size':
            parts.append(th.size_threshold(float(rest or 4)))
        elif kind == 'importance':
            column, _, per_tile = rest.partition(':')
            parts.append(th.importance(column, int(per_tile or 1000)))
        elif kind == 'tiny':
            parts.append(th.tiny_polygons(float(rest or th.TINY_PIXELS)))
        elif kind == 'cluster':
            parts.append(th.cluster(float(rest or 8)))
        else:
            raise click.BadParameter(f'unknown thinning {part!r}')
    return parts[0] if len(parts) == 1 else th.combine(*parts)


@click.group()
def cli():
    """Write GeoParquet files that a web map can read at tile cost."""


@cli.command('write')
@click.argument('src')
@click.argument('out')
@click.option(
    '--thinning',
    'thinning_spec',
    default='auto',
    show_default=True,
    help='auto | drop[:rate] | density[:budget] | smallest[:bytes] | size[:pixels] | tiny[:px] | importance:column[:n] | cluster[:px]; + unions, > chains',
)
@click.option(
    '--where', default=None, help='SQL predicate; rows failing it are never shown zoomed out'
)
@click.option('--columns', multiple=True, help='extra source columns the thinning reads')
@click.option(
    '--zooms',
    default='auto',
    show_default=True,
    help="inclusive range such as 2-16, or 'auto' to derive from the data",
)
@click.option(
    '--complete-from',
    default='auto',
    show_default=True,
    help="zoom from which every feature is present: a zoom, 'auto' (median feature spans a pixel) or 'never'",
)
@click.option(
    '--carry', multiple=True, help='column[:arrow type] to carry per LOD, e.g. risk:float32'
)
@click.option('--geometry', default='geometry', show_default=True)
@click.option('--prefix-zoom', default=6, type=int, show_default=True)
@click.option(
    '--no-prefix', is_flag=True, help='Use one global Hilbert order for indexed-file experiments'
)
@click.option('--row-group-rows', default=1_048_576, type=int, show_default=True)
@click.option('--page-bytes', default=16384, type=int, show_default=True)
@click.option('--compression-level', default=9, type=int, show_default=True, help='zstd level')
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
    complete_from,
    carry,
    geometry,
    prefix_zoom,
    no_prefix,
    row_group_rows,
    page_bytes,
    compression_level,
    batch_rows,
    workers,
    memory_limit,
    temp_dir,
):
    """Write SRC (GeoParquet with WKB geometry) to OUT in the LOD columns layout."""
    if zooms != 'auto':
        lo, _, hi = zooms.partition('-')
        zooms = range(int(lo), int(hi or lo) + 1)
    if complete_from != 'auto':
        complete_from = None if complete_from == 'never' else int(complete_from)
    carried = [tuple(c.split(':', 1)) if ':' in c else c for c in carry]
    write(
        src,
        out,
        thinning=parse_thinning(thinning_spec, where),
        columns=columns,
        zooms=zooms,
        complete_from=complete_from,
        carry=carried,
        geometry=geometry,
        prefix_zoom=None if no_prefix else prefix_zoom,
        row_group_rows=row_group_rows,
        page_bytes=page_bytes,
        compression_level=compression_level,
        batch_rows=batch_rows,
        workers=workers,
        memory_limit=memory_limit,
        temp_dir=temp_dir,
        log=click.echo,
    )


@cli.command('validate')
@click.argument('path')
def validate_cmd(path):
    """Check the layout contract and decode a sample of every LOD."""
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


@cli.command('optimize')
@click.argument('src', type=click.Path(exists=True, dir_okay=False))
@click.argument('out', type=click.Path(dir_okay=False))
@click.option('--leaf-pages', default=1024, type=click.IntRange(min=1), show_default=True)
@click.option(
    '--tile-pages/--keep-pages',
    default=True,
    show_default=True,
    help='Align LOD compression pages to tile boundaries, or just relocate existing pages',
)
@click.option(
    '--sparse-pages/--fixed-pages',
    default=True,
    show_default=True,
    help='Size sparse LOD pages by present values, or cap all pages at 2048 source rows',
)
def optimize_cmd(src, out, leaf_pages, tile_pages, sparse_pages):
    """Cluster LOD pages and add a spatial directory; preserve every table value."""
    from .optimize import optimize

    optimize(
        src,
        out,
        leaf_pages=leaf_pages,
        tile_pages=tile_pages,
        sparse_pages=sparse_pages,
        log=click.echo,
    )
