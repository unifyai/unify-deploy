"""OS map rendering primitives — WMTS tile fetcher + polygon overlay.

Three registered tools:

* ``valos_render_os_map`` — render a chosen OS layer for a geometry,
  optionally overlaying a boundary polygon.  The base building block.
* ``valos_render_location_plan`` — Land-Registry-style plan: outdoor
  layer + thick boundary outline + north arrow + scale bar.
* ``valos_render_competitor_map`` — outdoor layer + numbered pins for
  nearby competitors (e.g. care homes from Carterwood data).

All three return ``{"output_path": "...", "bbox": {...}, "layer": "...",
"projection": "...", "tile_count": N}`` on success.  PNGs are written
to the supplied filesystem path so ``ImageManager`` can resolve them
later via ``filter_images(filter="filepath == ...")``.

Implementation notes
--------------------
* OS Maps WMTS uses BNG (EPSG:27700) for the GB-only Outdoor / Light /
  Road / Leisure ``*_27700`` layers, and Web Mercator (EPSG:3857) for
  the global ``*_3857`` layers.  We pick the layer based on ``projection``.
* Tile-matrix resolutions for both projections are baked into
  ``_BNG_RESOLUTIONS`` / ``_WEBMERC_RESOLUTIONS`` below — these are the
  values OS publishes in their WMTS Capabilities documents.
* Polygon overlay assumes input GeoJSON is in WGS84 (lon/lat) — we
  reproject to the active tile-matrix CRS via ``pyproj`` before drawing.
"""

from __future__ import annotations

import math
from typing import Any

from unity.function_manager.custom import custom_function


# ---------------------------------------------------------------------------
# Tile-matrix metadata
# ---------------------------------------------------------------------------

# OS Maps API tile size and matrix origin.  Uniform across both projections.
_TILE_SIZE = 256

# British National Grid (EPSG:27700) origin and per-zoom resolution (m/pixel).
# Values from the OS Maps API WMTS Capabilities document.
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

# Web Mercator (EPSG:3857) — standard slippy-map origin, 156543.03... m/pixel
# at zoom 0 then halving each zoom.
_WEBMERC_ORIGIN = (-20037508.342789244, 20037508.342789244)
_WEBMERC_BASE_RESOLUTION = 156543.03392804097
_WEBMERC_RESOLUTIONS: list[float] = [
    _WEBMERC_BASE_RESOLUTION / (2**z) for z in range(20)
]

# OS layer naming convention: <Style>_<EPSG>.  We default to Outdoor
# because it gives the closest match to the Valos location-plan style.
_DEFAULT_LAYERS = {"EPSG:27700": "Outdoor_27700", "EPSG:3857": "Outdoor_3857"}


# ---------------------------------------------------------------------------
# Public renderers
# ---------------------------------------------------------------------------


@custom_function()
async def valos_render_os_map(
    geometry: dict,
    output_path: str,
    layer: str | None = None,
    projection: str = "EPSG:27700",
    zoom: int = 8,
    boundary_polygon: dict | None = None,
    boundary_colour: str = "#d62728",
    boundary_width: int = 4,
) -> dict:
    """Render an OS Maps WMTS layer to a PNG.

    Parameters
    ----------
    geometry : dict
        GeoJSON ``{"type": "Polygon", "coordinates": ...}`` or a centre
        spec ``{"type": "Point", "coordinates": [lon, lat], "buffer_m": 250}``
        in WGS84.  The bounding box of the geometry (plus its buffer
        for points) defines the rendered extent.
    output_path : str
        Filesystem path the PNG is written to.  Workspace-relative paths
        are resolved against the current working directory.  The directory
        must exist.
    layer : str, optional
        OS Maps layer name, e.g. ``"Outdoor_27700"``, ``"Light_3857"``,
        ``"Road_27700"``, ``"Leisure_27700"``.  Defaults to the
        projection-appropriate Outdoor layer.
    projection : str, optional
        ``"EPSG:27700"`` (British National Grid) or ``"EPSG:3857"``
        (Web Mercator).  Defaults to BNG, which gives crisper rendering
        of GB content.
    zoom : int, optional
        Tile-matrix level.  0 is national-scale, higher = closer-in.
        Defaults to 8 (street-level for most areas).
    boundary_polygon : dict, optional
        Additional GeoJSON polygon to overlay on the rendered tiles
        (e.g. a Land-Registry title boundary).  Reprojected from WGS84
        to the active CRS before drawing.
    boundary_colour : str, optional
        Hex colour for the boundary outline.  Defaults to
        ``"#d62728"`` (Tableau red).
    boundary_width : int, optional
        Outline thickness in pixels.  Defaults to 4.

    Returns
    -------
    dict
        On success::

            {
                "output_path": "...",
                "bbox": {"min_x": ..., "min_y": ..., "max_x": ..., "max_y": ...},
                "projection": "EPSG:27700",
                "layer": "Outdoor_27700",
                "zoom": 8,
                "tile_count": 12,
                "image_size_px": [768, 768],
            }

        On failure: ``{"error", ...}``.
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


@custom_function()
async def valos_render_location_plan(
    polygon: dict,
    output_path: str,
    projection: str = "EPSG:27700",
    zoom: int = 9,
    north_arrow: bool = True,
    scale_bar: bool = True,
) -> dict:
    """Land-Registry-style location plan for a single title polygon.

    Calls ``valos_render_os_map`` with the Outdoor layer and a thick
    red boundary, then optionally overlays a north arrow and scale bar.

    Parameters
    ----------
    polygon : dict
        GeoJSON polygon in WGS84 — typically the polygon returned by
        ``valos_get_title_polygon``.
    output_path : str
        Where to write the PNG.
    projection : str, optional
        ``"EPSG:27700"`` (default) or ``"EPSG:3857"``.
    zoom : int, optional
        Tile-matrix level.  Defaults to 9 (close-in, building-scale).
    north_arrow : bool, optional
        Draw a north arrow in the top-right corner.  Defaults to True.
    scale_bar : bool, optional
        Draw a scale bar in the bottom-left corner.  Defaults to True.

    Returns
    -------
    dict
        Same shape as :func:`valos_render_os_map`.
    """
    result = await valos_render_os_map(
        geometry=polygon,
        output_path=output_path,
        layer=_DEFAULT_LAYERS[projection],
        projection=projection,
        zoom=zoom,
        boundary_polygon=polygon,
        boundary_colour="#c0392b",
        boundary_width=5,
    )
    if "error" in result:
        return result

    if north_arrow or scale_bar:
        _add_decorations(
            output_path,
            projection=projection,
            zoom=zoom,
            north_arrow=north_arrow,
            scale_bar=scale_bar,
        )
    return result


@custom_function()
async def valos_render_competitor_map(
    centre: list,
    radius_km: float,
    pins: list,
    output_path: str,
    projection: str = "EPSG:27700",
    zoom: int = 7,
) -> dict:
    """Render an OS map with numbered pins for nearby competitors.

    Parameters
    ----------
    centre : list[float]
        ``[lon, lat]`` of the subject property.
    radius_km : float
        Catchment radius in kilometres; defines the rendered extent.
    pins : list[dict]
        Competitor entries, each ``{"label": "1", "lon": ..., "lat": ...,
        "name": "Acacia House", "cqc_rating": "Good"}``.  The ``label``
        is drawn on the pin; the rest is metadata for the report layer.
    output_path : str
        Where to write the PNG.
    projection : str, optional
        ``"EPSG:27700"`` (default) or ``"EPSG:3857"``.
    zoom : int, optional
        Tile-matrix level.  Defaults to 7 (catchment-area scale).

    Returns
    -------
    dict
        Same shape as :func:`valos_render_os_map`, plus
        ``"pins_drawn": N``.
    """
    geometry = {
        "type": "Point",
        "coordinates": list(centre),
        "buffer_m": radius_km * 1000.0,
    }

    result = await valos_render_os_map(
        geometry=geometry,
        output_path=output_path,
        layer=_DEFAULT_LAYERS[projection],
        projection=projection,
        zoom=zoom,
    )
    if "error" in result:
        return result

    drawn = _draw_pins_on_image(
        output_path,
        pins=pins,
        projection=projection,
        zoom=zoom,
        bbox_in_crs=result["bbox"],
    )
    result["pins_drawn"] = drawn
    return result


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
        # Approximate degree size of the buffer at this latitude.  Only
        # used to seed the tile range — the renderer reprojects via pyproj
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

    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._os_client import (
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
        px = (x_crs - bbox_in_crs["min_x"]) / (
            bbox_in_crs["max_x"] - bbox_in_crs["min_x"]
        ) * width
        py = (bbox_in_crs["max_y"] - y_crs) / (
            bbox_in_crs["max_y"] - bbox_in_crs["min_y"]
        ) * height
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
            draw.rectangle([(x0, y0), (x0 + length_px, y0 + 8)], outline="black", width=2)
            draw.rectangle(
                [(x0, y0), (x0 + length_px // 2, y0 + 8)], fill="black",
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
