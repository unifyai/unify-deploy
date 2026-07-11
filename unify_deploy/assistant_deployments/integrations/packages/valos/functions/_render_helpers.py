"""Internal helpers for the OS map renderers in ``render.py``.

Underscore-prefixed so :func:`unify.function_manager.custom_functions.collect_custom_functions`
skips this file.  Holds the tile-matrix constants, geometry math, WMTS
stitching, polygon and pin overlays, and the shared async render
implementation that all three public renderers in ``render.py`` wrap.

Implementation notes
--------------------
* OS Maps WMTS uses BNG (EPSG:27700) for the GB-only Outdoor / Light /
  Road / Leisure ``*_27700`` layers, and Web Mercator (EPSG:3857) for
  the global ``*_3857`` layers.
* Tile-matrix resolutions for both projections are baked into
  ``_BNG_RESOLUTIONS`` / ``_WEBMERC_RESOLUTIONS`` — the values OS
  publishes in their WMTS Capabilities documents.
* Polygon overlay assumes input GeoJSON is in WGS84 (lon/lat); the
  drawer reprojects to the active tile-matrix CRS via ``pyproj`` before
  rasterising.
"""

from __future__ import annotations

import math
from typing import Any

# ---------------------------------------------------------------------------
# Tile-matrix metadata
# ---------------------------------------------------------------------------

_TILE_SIZE = 256

_BNG_ORIGIN = (-238375.0, 1376256.0)
_BNG_RESOLUTIONS: list[float] = [
    896.0,
    448.0,
    224.0,
    112.0,
    56.0,
    28.0,
    14.0,
    7.0,
    3.5,
    1.75,
    0.875,
    0.4375,
    0.21875,
    0.109375,
]

_WEBMERC_ORIGIN = (-20037508.342789244, 20037508.342789244)
_WEBMERC_BASE_RESOLUTION = 156543.03392804097
_WEBMERC_RESOLUTIONS: list[float] = [
    _WEBMERC_BASE_RESOLUTION / (2**z) for z in range(20)
]

_DEFAULT_LAYERS = {"EPSG:27700": "Outdoor_27700", "EPSG:3857": "Outdoor_3857"}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _bbox_from_geometry(geometry: dict) -> dict:
    """Return the WGS84 bbox of a GeoJSON Polygon or buffered Point."""
    if not isinstance(geometry, dict) or "type" not in geometry:
        return {"error": "geometry must be a GeoJSON Polygon or Point dict."}

    if geometry["type"] == "Polygon":
        coords = geometry.get("coordinates") or []
        if not coords or not coords[0]:
            return {"error": "Polygon has no coordinates."}
        ring = coords[0]
        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]
        return {
            "min_x": min(lons),
            "min_y": min(lats),
            "max_x": max(lons),
            "max_y": max(lats),
        }

    if geometry["type"] == "Point":
        coords = geometry.get("coordinates") or []
        if len(coords) != 2:
            return {"error": "Point coordinates must be [lon, lat]."}
        lon, lat = coords
        buffer_m = float(geometry.get("buffer_m", 250.0))
        # Approximate degree size of the buffer at this latitude — used
        # only to seed the tile range; the renderer reprojects via pyproj
        # for accurate pixel placement.
        deg_lat = buffer_m / 111_320.0
        deg_lon = buffer_m / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
        return {
            "min_x": lon - deg_lon,
            "min_y": lat - deg_lat,
            "max_x": lon + deg_lon,
            "max_y": lat + deg_lat,
        }

    return {"error": f"Unsupported geometry type '{geometry['type']}'."}


def _circle_ring_wgs84(
    lon: float,
    lat: float,
    radius_km: float,
    *,
    segments: int = 128,
) -> dict:
    """Build a GeoJSON polygon approximating a circle of ``radius_km``.

    Used by the catchment renderer.  The ring doubles as the geometry
    whose bbox sets the render extent *and* the boundary polygon drawn
    on the tiles — so exactly one circle is ever produced, sized to the
    requested radius.  Equirectangular offset is accurate to well under
    a pixel at care-home catchment scale (a few km).
    """
    radius_m = radius_km * 1000.0
    lat_r = math.radians(lat)
    metres_per_deg_lon = 111_320.0 * max(0.1, math.cos(lat_r))
    ring: list[list[float]] = []
    for i in range(segments + 1):
        angle = 2 * math.pi * i / segments
        dx = radius_m * math.cos(angle)
        dy = radius_m * math.sin(angle)
        ring.append(
            [
                lon + dx / metres_per_deg_lon,
                lat + dy / 111_320.0,
            ],
        )
    return {"type": "Polygon", "coordinates": [ring]}


def _reproject_bbox(bbox: dict, src_crs: str, dst_crs: str) -> dict:
    """Reproject a bbox from src_crs to dst_crs via pyproj."""
    if src_crs == dst_crs:
        return dict(bbox)

    from pyproj import Transformer

    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    min_x, min_y = transformer.transform(bbox["min_x"], bbox["min_y"])
    max_x, max_y = transformer.transform(bbox["max_x"], bbox["max_y"])
    return {
        "min_x": min(min_x, max_x),
        "min_y": min(min_y, max_y),
        "max_x": max(min_x, max_x),
        "max_y": max(min_y, max_y),
    }


def _bbox_to_tile_range(bbox: dict, *, projection: str, zoom: int) -> dict:
    """Map a CRS-space bbox to a (col, row) tile-range at the chosen zoom.

    Returns ``{"col_min", "col_max", "row_min", "row_max", "count"}``.
    """
    origin, resolution = _matrix_origin_and_resolution(projection, zoom)
    if origin is None:
        return {"error": f"Zoom {zoom} out of range for {projection}."}

    pixel_size = resolution * _TILE_SIZE
    col_min = int(math.floor((bbox["min_x"] - origin[0]) / pixel_size))
    col_max = int(math.floor((bbox["max_x"] - origin[0]) / pixel_size))
    row_min = int(math.floor((origin[1] - bbox["max_y"]) / pixel_size))
    row_max = int(math.floor((origin[1] - bbox["min_y"]) / pixel_size))

    col_min, col_max = sorted([col_min, col_max])
    row_min, row_max = sorted([row_min, row_max])
    count = (col_max - col_min + 1) * (row_max - row_min + 1)

    return {
        "col_min": col_min,
        "col_max": col_max,
        "row_min": row_min,
        "row_max": row_max,
        "count": count,
        "origin": origin,
        "resolution": resolution,
        "pixel_size": pixel_size,
    }


def _matrix_origin_and_resolution(
    projection: str,
    zoom: int,
) -> tuple[tuple[float, float] | None, float]:
    if projection == "EPSG:27700":
        if 0 <= zoom < len(_BNG_RESOLUTIONS):
            return _BNG_ORIGIN, _BNG_RESOLUTIONS[zoom]
    elif projection == "EPSG:3857":
        if 0 <= zoom < len(_WEBMERC_RESOLUTIONS):
            return _WEBMERC_ORIGIN, _WEBMERC_RESOLUTIONS[zoom]
    return None, 0.0


# ---------------------------------------------------------------------------
# Tile fetching + stitching
# ---------------------------------------------------------------------------


async def _stitch_wmts_tiles(
    *,
    layer: str,
    projection: str,
    zoom: int,
    tile_range: dict,
) -> Any:
    """Fetch every tile in ``tile_range`` and stitch into a single image."""
    from io import BytesIO

    from PIL import Image

    from unify_deploy.assistant_deployments.integrations.packages.valos.functions._os_client import (
        os_fetch_wmts_tile,
    )

    cols = range(tile_range["col_min"], tile_range["col_max"] + 1)
    rows = range(tile_range["row_min"], tile_range["row_max"] + 1)

    width = (tile_range["col_max"] - tile_range["col_min"] + 1) * _TILE_SIZE
    height = (tile_range["row_max"] - tile_range["row_min"] + 1) * _TILE_SIZE
    canvas = Image.new("RGB", (width, height), color="white")

    for col in cols:
        for row in rows:
            tile_bytes = await os_fetch_wmts_tile(
                layer=layer,
                tile_matrix_set=projection,
                tile_matrix=str(zoom),
                tile_row=row,
                tile_col=col,
            )
            if isinstance(tile_bytes, dict):
                return tile_bytes
            tile_img = Image.open(BytesIO(tile_bytes)).convert("RGB")
            canvas.paste(
                tile_img,
                (
                    (col - tile_range["col_min"]) * _TILE_SIZE,
                    (row - tile_range["row_min"]) * _TILE_SIZE,
                ),
            )
    return canvas


# ---------------------------------------------------------------------------
# Polygon and pin overlay
# ---------------------------------------------------------------------------


def _draw_polygon_on_image(
    *,
    image,
    polygon_geojson: dict,
    tile_range: dict,
    projection: str,
    zoom: int,
    outline_colour: str,
    outline_width: int,
) -> None:
    """Reproject a WGS84 polygon into pixel space and draw its outline."""
    from PIL import ImageDraw

    coords = polygon_geojson.get("coordinates") or []
    if not coords or not coords[0]:
        return

    pixel_ring = [
        _crs_to_pixel(
            *_wgs84_to_crs(lon, lat, projection),
            tile_range=tile_range,
        )
        for lon, lat in coords[0]
    ]
    draw = ImageDraw.Draw(image)
    draw.line(pixel_ring + [pixel_ring[0]], fill=outline_colour, width=outline_width)


def _draw_pins_on_image(
    output_path: str,
    *,
    pins: list,
    projection: str,
    zoom: int,
    bbox_in_crs: dict,
) -> int:
    """Reload the rendered PNG and overlay numbered pins.

    Returns the number of pins drawn (those whose coordinates fell
    inside the rendered bbox).
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(output_path).convert("RGB")
    width = img.width
    height = img.height

    origin, resolution = _matrix_origin_and_resolution(projection, zoom)
    if origin is None:
        return 0

    drawn = 0
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    for pin in pins:
        lon = pin.get("lon")
        lat = pin.get("lat")
        if lon is None or lat is None:
            continue
        x_crs, y_crs = _wgs84_to_crs(lon, lat, projection)
        if not (
            bbox_in_crs["min_x"] <= x_crs <= bbox_in_crs["max_x"]
            and bbox_in_crs["min_y"] <= y_crs <= bbox_in_crs["max_y"]
        ):
            continue
        px = (
            (x_crs - bbox_in_crs["min_x"])
            / (bbox_in_crs["max_x"] - bbox_in_crs["min_x"])
            * width
        )
        py = (
            (bbox_in_crs["max_y"] - y_crs)
            / (bbox_in_crs["max_y"] - bbox_in_crs["min_y"])
            * height
        )
        radius = 12
        draw.ellipse(
            [(px - radius, py - radius), (px + radius, py + radius)],
            fill="#1f77b4",
            outline="white",
            width=2,
        )
        label = str(pin.get("label", drawn + 1))
        if font is not None:
            draw.text((px - 4, py - 6), label, fill="white", font=font)
        drawn += 1

    img.save(output_path, format="PNG")
    return drawn


def _add_decorations(
    output_path: str,
    *,
    projection: str,
    zoom: int,
    north_arrow: bool,
    scale_bar: bool,
) -> None:
    """Overlay a north arrow and/or scale bar onto an existing PNG."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(output_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    if north_arrow:
        x0 = img.width - 60
        y0 = 20
        draw.polygon(
            [(x0, y0 + 30), (x0 + 12, y0), (x0 + 24, y0 + 30), (x0 + 12, y0 + 22)],
            fill="black",
        )
        if font is not None:
            draw.text((x0 + 6, y0 + 32), "N", fill="black", font=font)

    if scale_bar:
        _, resolution = _matrix_origin_and_resolution(projection, zoom)
        if resolution > 0:
            target_metres = _round_scale_bar(resolution * 100)
            length_px = int(target_metres / resolution)
            x0 = 20
            y0 = img.height - 30
            draw.rectangle(
                [(x0, y0), (x0 + length_px, y0 + 8)],
                outline="black",
                width=2,
            )
            draw.rectangle(
                [(x0, y0), (x0 + length_px // 2, y0 + 8)],
                fill="black",
            )
            if font is not None:
                label = (
                    f"{int(target_metres)} m"
                    if target_metres < 1000
                    else f"{target_metres / 1000:g} km"
                )
                draw.text((x0, y0 - 14), label, fill="black", font=font)

    img.save(output_path, format="PNG")


def _round_scale_bar(metres: float) -> float:
    """Round a metric distance to a presentable 1/2/5 * 10^n value."""
    if metres <= 0:
        return 100.0
    exponent = math.floor(math.log10(metres))
    base = 10**exponent
    for multiplier in (1, 2, 5, 10):
        candidate = multiplier * base
        if candidate >= metres:
            return candidate
    return 10 * base


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------


def _wgs84_to_crs(lon: float, lat: float, projection: str) -> tuple[float, float]:
    if projection == "EPSG:4326":
        return lon, lat
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", projection, always_xy=True)
    return transformer.transform(lon, lat)


def _crs_to_pixel(x: float, y: float, *, tile_range: dict) -> tuple[float, float]:
    """Map a CRS-space coordinate to absolute pixel coordinates on the
    stitched image."""
    origin = tile_range["origin"]
    resolution = tile_range["resolution"]
    px = (x - origin[0]) / resolution - tile_range["col_min"] * _TILE_SIZE
    py = (origin[1] - y) / resolution - tile_range["row_min"] * _TILE_SIZE
    return px, py


# ---------------------------------------------------------------------------
# Shared render-impl helper — wrapped by all three public renderers in render.py
# ---------------------------------------------------------------------------


async def _render_os_map_impl(
    *,
    geometry: dict,
    output_path: str,
    layer: str | None,
    projection: str,
    zoom: int,
    boundary_polygon: dict | None,
    boundary_colour: str,
    boundary_width: int,
) -> dict:
    """Fetch + stitch WMTS tiles, optionally overlay a polygon, save PNG.

    The three ``valos_render_*`` tools in ``render.py`` are thin
    wrappers around this — they just set their own parameter defaults
    and apply post-render decorations (north arrow / pins / etc.).
    """
    bbox = _bbox_from_geometry(geometry)
    if "error" in bbox:
        return bbox

    chosen_layer = layer or _DEFAULT_LAYERS.get(projection)
    if chosen_layer is None:
        return {"error": f"Unsupported projection '{projection}'."}

    bbox_in_crs = _reproject_bbox(bbox, "EPSG:4326", projection)
    tiles = _bbox_to_tile_range(bbox_in_crs, projection=projection, zoom=zoom)
    if "error" in tiles:
        return tiles

    img_or_err = await _stitch_wmts_tiles(
        layer=chosen_layer,
        projection=projection,
        zoom=zoom,
        tile_range=tiles,
    )
    if isinstance(img_or_err, dict):
        return img_or_err

    if boundary_polygon is not None:
        _draw_polygon_on_image(
            image=img_or_err,
            polygon_geojson=boundary_polygon,
            tile_range=tiles,
            projection=projection,
            zoom=zoom,
            outline_colour=boundary_colour,
            outline_width=boundary_width,
        )

    img_or_err.save(output_path, format="PNG")
    return {
        "output_path": output_path,
        "bbox": bbox_in_crs,
        "projection": projection,
        "layer": chosen_layer,
        "zoom": zoom,
        "tile_count": tiles["count"],
        "image_size_px": [img_or_err.width, img_or_err.height],
    }
