"""OS map rendering primitives — four @custom_function-decorated tools.

* ``valos_render_os_map`` — render a chosen OS layer for a geometry,
  optionally overlaying a boundary polygon.  The base building block.
* ``valos_render_location_plan`` — Land-Registry-style plan: outdoor
  layer + thick boundary outline + north arrow + scale bar.
* ``valos_render_competitor_map`` — outdoor layer + numbered pins for
  nearby competitors (e.g. care homes from Carterwood data).
* ``valos_render_catchment_map`` — outdoor layer + a single labelled
  catchment circle of a chosen radius + a subject marker.

All return ``{"output_path": "...", "bbox": {...}, "layer": "...",
"projection": "...", "tile_count": N}`` on success.  PNGs are written
to the supplied filesystem path so ``ImageManager`` can resolve them
later via ``filter_images(filter="filepath == ...")``.

Internal helpers (tile-matrix constants, geometry math, WMTS stitching,
polygon and pin overlays, and the shared async render impl) live in
``_render_helpers.py``.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


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
    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._render_helpers import (
        _render_os_map_impl,
    )

    return await _render_os_map_impl(
        geometry=geometry,
        output_path=output_path,
        layer=layer,
        projection=projection,
        zoom=zoom,
        boundary_polygon=boundary_polygon,
        boundary_colour=boundary_colour,
        boundary_width=boundary_width,
    )


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

    Renders the projection-appropriate OS Outdoor layer with a thick
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
    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._render_helpers import (
        _DEFAULT_LAYERS,
        _add_decorations,
        _render_os_map_impl,
    )

    result = await _render_os_map_impl(
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
    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._render_helpers import (
        _DEFAULT_LAYERS,
        _draw_pins_on_image,
        _render_os_map_impl,
    )

    geometry = {
        "type": "Point",
        "coordinates": list(centre),
        "buffer_m": radius_km * 1000.0,
    }

    result = await _render_os_map_impl(
        geometry=geometry,
        output_path=output_path,
        layer=_DEFAULT_LAYERS[projection],
        projection=projection,
        zoom=zoom,
        boundary_polygon=None,
        boundary_colour="#d62728",
        boundary_width=4,
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


@custom_function()
async def valos_render_catchment_map(
    centre: list,
    radius_km: float,
    output_path: str,
    projection: str = "EPSG:27700",
    zoom: int = 7,
    circle_colour: str = "#1f4eb4",
    circle_width: int = 3,
    subject_marker: bool = True,
) -> dict:
    """Render an OS map with a single labelled catchment circle.

    Draws exactly one ring at ``radius_km`` around ``centre``.  The same
    circle geometry both sets the rendered extent and is drawn on the
    tiles, so the output can never contain a stray second circle — pass
    one radius and get one ring.

    Parameters
    ----------
    centre : list[float]
        ``[lon, lat]`` of the subject property (WGS84).
    radius_km : float
        Catchment radius in kilometres (e.g. 4.83 for a 3-mile catchment).
    output_path : str
        Where to write the PNG.
    projection : str, optional
        ``"EPSG:27700"`` (default) or ``"EPSG:3857"``.
    zoom : int, optional
        Tile-matrix level.  Defaults to 7 (catchment-area scale).
    circle_colour : str, optional
        Hex colour for the catchment ring.  Defaults to ``"#1f4eb4"``.
    circle_width : int, optional
        Ring thickness in pixels.  Defaults to 3.
    subject_marker : bool, optional
        Draw a marker at ``centre``.  Defaults to True.

    Returns
    -------
    dict
        Same shape as :func:`valos_render_os_map`.
    """
    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._render_helpers import (
        _DEFAULT_LAYERS,
        _add_decorations,
        _circle_ring_wgs84,
        _draw_pins_on_image,
        _render_os_map_impl,
    )

    lon, lat = centre[0], centre[1]
    circle = _circle_ring_wgs84(lon, lat, radius_km)

    result = await _render_os_map_impl(
        geometry=circle,
        output_path=output_path,
        layer=_DEFAULT_LAYERS[projection],
        projection=projection,
        zoom=zoom,
        boundary_polygon=circle,
        boundary_colour=circle_colour,
        boundary_width=circle_width,
    )
    if "error" in result:
        return result

    if subject_marker:
        _draw_pins_on_image(
            output_path,
            pins=[{"label": "S", "lon": lon, "lat": lat}],
            projection=projection,
            zoom=zoom,
            bbox_in_crs=result["bbox"],
        )

    _add_decorations(
        output_path,
        projection=projection,
        zoom=zoom,
        north_arrow=True,
        scale_bar=True,
    )
    result["radius_km"] = radius_km
    return result
