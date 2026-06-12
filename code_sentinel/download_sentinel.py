"""
Sentinel-2 L2A acquisition-by-acquisition downloader.

What it does
------------
- Reads polygons from a shapefile.
- Searches the CDSE STAC catalog for Sentinel-2 L2A acquisitions.
- Keeps only items whose eo:cloud_cover is below your threshold.
- For each matching acquisition and polygon:
    * sends a Process API request clipped to the polygon
    * writes a final GeoTIFF directly
    * computes summary stats
    * appends a row to a CSV catalogue

No "download full image then clip locally" step.

Auth
----
Set these environment variables:
    CDSE_CLIENT_ID
    CDSE_CLIENT_SECRET

Dependencies
------------
    pip install requests requests-oauthlib geopandas shapely rasterio sentinelhub numpy pandas
"""

from __future__ import annotations

import csv
import json
import logging
import logging.handlers
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from oauthlib.oauth2 import BackendApplicationClient
from requests_oauthlib import OAuth2Session
from rasterio.transform import from_bounds
from sentinelhub import BBox, CRS, bbox_to_dimensions
from shapely.geometry import mapping
from shapely.ops import orient

# ------------------------------------------------------------------
# USER SETTINGS
# ------------------------------------------------------------------

SHAPEFILE = r"C:/Users/reub0539/OneDrive - Nexus365/Dphil/Projects/Project3/NFI_50ha/wytham_bagley.shp"
OUTPUT_DIR = r"C:/Users/reub0539/work/Sentinel_LSP/data/wytham_test"
POLYGON_ID_FIELD = "forest"

START_DATE = "2025-03-01"
END_DATE = "2025-06-30"

CLOUD_THRESHOLD = 20.0  # percent
RESOLUTION_M = 10
NODATA_VALUE = -9999.0

# If your polygons are huge, reduce this.
MAX_WORKERS = 4

# ------------------------------------------------------------------
# ENDPOINTS
# ------------------------------------------------------------------

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
STAC_SEARCH_URL = "https://stac.dataspace.copernicus.eu/v1/search"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"

# ------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(os.path.join(OUTPUT_DIR, "metadata"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            os.path.join(OUTPUT_DIR, "sentinel_download.log"),
            maxBytes=10_000_000,
            backupCount=5,
            encoding="utf-8",
        ),
        logging.StreamHandler(
            stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
        ),
    ],
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# EVALSCRIPT
# ------------------------------------------------------------------

# Conservative masking using SCL, based on the official Sentinel-2 L2A examples
# and the documented invalid-observation classes for the mosaic algorithm.
EVALSCRIPT = f"""
//VERSION=3
function setup() {{
  return {{
    input: [{{ bands: ["B02", "B03", "B04", "B08", "SCL"], units: "REFLECTANCE" }}],
    output: {{
      bands: 4,
      sampleType: "FLOAT32"
    }}
  }};
}}

function evaluatePixel(sample) {{
  // Mask out invalid/cloudy observations conservatively.
  if ([1, 3, 7, 8, 9, 10].includes(sample.SCL)) {{
    return [{NODATA_VALUE}, {NODATA_VALUE}, {NODATA_VALUE}, {NODATA_VALUE}];
  }}
  return [sample.B02, sample.B03, sample.B04, sample.B08];
}}
"""

# ------------------------------------------------------------------
# AUTH
# ------------------------------------------------------------------

def build_oauth_session() -> OAuth2Session:
    client_id = os.getenv("CDSE_CLIENT_ID")
    client_secret = os.getenv("CDSE_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError("Set CDSE_CLIENT_ID and CDSE_CLIENT_SECRET environment variables first.")

    client = BackendApplicationClient(client_id=client_id)
    oauth = OAuth2Session(client=client)
    oauth.fetch_token(
        token_url=TOKEN_URL,
        client_secret=client_secret,
        include_client_id=True,
    )
    return oauth

# ------------------------------------------------------------------
# GEOMETRY / IO HELPERS
# ------------------------------------------------------------------

def sanitize_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(text))

def orient_and_geojson(geom) -> dict:
    geom = orient(geom, sign=1.0)
    return mapping(geom)

def month_or_year_window(start_date: str, end_date: str) -> str:
    return f"{start_date}T00:00:00Z/{end_date}T23:59:59Z"

def compute_stats(path: str) -> dict:
    with rasterio.open(path) as src:
        data = src.read().astype(np.float32)

    valid = data[0] != NODATA_VALUE
    valid_count = int(valid.sum())

    if valid_count == 0:
        return {
            "valid_pixel_count": 0,
            "mean_blue": None,
            "mean_green": None,
            "mean_red": None,
            "mean_nir": None,
            "blue_nir_ratio": None,
            "ndvi_mean": None,
            "ndvi_sd": None,
        }

    blue = data[0][valid]
    green = data[1][valid]
    red = data[2][valid]
    nir = data[3][valid]
    ndvi = (nir - red) / (nir + red + 1e-6)

    mean_blue = float(np.mean(blue))
    mean_green = float(np.mean(green))
    mean_red = float(np.mean(red))
    mean_nir = float(np.mean(nir))

    return {
        "valid_pixel_count": valid_count,
        "mean_blue": mean_blue,
        "mean_green": mean_green,
        "mean_red": mean_red,
        "mean_nir": mean_nir,
        "blue_nir_ratio": float(mean_blue / mean_nir) if mean_nir > 0 else None,
        "ndvi_mean": float(np.mean(ndvi)),
        "ndvi_sd": float(np.std(ndvi)),
    }

def write_tiff(path: str, array: np.ndarray, geom) -> None:
    if array.ndim != 3:
        raise ValueError(f"Expected (bands, rows, cols), got {array.shape}")

    bbox = BBox(bbox=geom.bounds, crs=CRS.WGS84)
    width, height = array.shape[2], array.shape[1]
    transform = from_bounds(*geom.bounds, width=width, height=height)

    profile = {
        "driver": "GTiff",
        "height": array.shape[1],
        "width": array.shape[2],
        "count": array.shape[0],
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": NODATA_VALUE,
        "compress": "deflate",
        "tiled": True,
    }

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array.astype(np.float32))

# ------------------------------------------------------------------
# STAC SEARCH
# ------------------------------------------------------------------

def stac_search_all(oauth: OAuth2Session, geom_geojson: dict) -> list[dict]:
    """
    Search STAC for all Sentinel-2 L2A items intersecting the polygon,
    within the date range, and below the cloud threshold.
    """
    body = {
        "collections": ["sentinel-2-l2a"],
        "datetime": month_or_year_window(START_DATE, END_DATE),
        "intersects": geom_geojson,
        "query": {
            "eo:cloud_cover": {"lt": CLOUD_THRESHOLD},
        },
        "limit": 100,
    }

    items = []
    url = STAC_SEARCH_URL
    first = True

    while url:
        resp = oauth.post(url, json=body if first else None)
        resp.raise_for_status()
        data = resp.json()

        items.extend(data.get("features", []))

        next_url = None
        for link in data.get("links", []):
            if link.get("rel") == "next":
                next_url = link.get("href")
                break

        url = next_url
        first = False

    return items

# ------------------------------------------------------------------
# PROCESS API
# ------------------------------------------------------------------

def process_item(
    oauth: OAuth2Session,
    polygon_geom,
    item: dict,
    out_path: str,
) -> dict:
    """
    Process one Sentinel-2 acquisition for one polygon.

    We use a very small time window around the acquisition timestamp.
    The docs note that time intervals shorter than 50 minutes avoid overlap
    between different acquisitions, even near the poles.
    """
    item_dt = item["properties"]["datetime"]
    dt = datetime.fromisoformat(item_dt.replace("Z", "+00:00"))
    window_from = (dt - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_to = (dt + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    geom_geojson = orient_and_geojson(polygon_geom)

    bbox = BBox(bbox=polygon_geom.bounds, crs=CRS.WGS84)
    size = bbox_to_dimensions(bbox, resolution=RESOLUTION_M)

    # Safety check: if this is too large, reduce resolution or split polygon.
    if size[0] > 2500 or size[1] > 2500:
        raise RuntimeError(
            f"Output too large for Process API at {size[0]}x{size[1]}. "
            f"Reduce RESOLUTION_M or split the polygon."
        )

    request_body = {
        "input": {
            "bounds": {
                "properties": {
                    "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
                },
                "geometry": geom_geojson,
            },
            "data": [
                {
                    "type": "sentinel-2-l2a",
                    "dataFilter": {
                        "timeRange": {
                            "from": window_from,
                            "to": window_to,
                        }
                    },
                }
            ],
        },
        "output": {
            "width": size[0],
            "height": size[1],
            "responses": [
                {"identifier": "default", "format": {"type": "image/tiff"}}
            ],
        },
        "evalscript": EVALSCRIPT,
    }

    resp = oauth.post(PROCESS_URL, json=request_body, headers={"Accept": "image/tiff"})
    if resp.status_code != 200:
        log.error(
            "Process failed for item %s (%s): %s",
            item.get("id"),
            item_dt,
            resp.text[:2000],
        )
        resp.raise_for_status()

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(resp.content)

    return compute_stats(out_path)

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def read_polygons(shapefile: str, id_field: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(shapefile)

    if gdf.crs is None:
        raise ValueError("Shapefile has no CRS defined")

    if gdf.crs.to_epsg() != 4326:
        log.info("Reprojecting from %s to EPSG:4326", gdf.crs)
        gdf = gdf.to_crs(epsg=4326)

    if id_field not in gdf.columns:
        log.warning(
            "Field '%s' not found. Available fields: %s. Falling back to row index.",
            id_field,
            gdf.columns.tolist(),
        )

    rows = []
    for idx, row in gdf.iterrows():
        name = str(row[id_field]) if id_field in gdf.columns else f"polygon_{idx:04d}"
        geom = row.geometry

        if geom.is_empty:
            continue

        if geom.geom_type == "Polygon":
            rows.append({"polygon_id": name, "geometry": geom})
        elif geom.geom_type == "MultiPolygon":
            for i, poly in enumerate(geom.geoms, start=1):
                rows.append({"polygon_id": f"{name}_part{i}", "geometry": poly})
        else:
            log.warning("Skipping unsupported geometry type '%s' for %s", geom.geom_type, name)

    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")

def main() -> None:
    oauth = build_oauth_session()
    polygons = read_polygons(SHAPEFILE, POLYGON_ID_FIELD)

    catalogue_rows = []
    metadata_dir = os.path.join(OUTPUT_DIR, "metadata")
    os.makedirs(metadata_dir, exist_ok=True)

    for _, prow in polygons.iterrows():
        poly_id = prow["polygon_id"]
        geom = prow.geometry
        poly_name = sanitize_name(poly_id)

        log.info("Searching acquisitions for polygon: %s", poly_id)
        items = stac_search_all(oauth, geom)

        log.info("Polygon %s: %d acquisitions below cloud threshold %.1f%%",
                 poly_id, len(items), CLOUD_THRESHOLD)

        for item in items:
            dt = item["properties"]["datetime"]
            platform = item["properties"].get("platform", "")
            item_id = item.get("id", "")

            stamp = dt[:10].replace("-", "") + "_" + dt[11:19].replace(":", "")
            out_dir = os.path.join(OUTPUT_DIR, poly_name, dt[:4])
            out_name = f"a{poly_name}_{stamp}_{platform}.tif"
            out_path = os.path.join(out_dir, out_name)

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                log.info("Skipping existing output: %s", out_path)
                stats = compute_stats(out_path)
            else:
                try:
                    stats = process_item(oauth, geom, item, out_path)
                    log.info("Wrote %s", out_path)
                except Exception as exc:
                    log.error(
                        "Failed polygon=%s item=%s datetime=%s: %s",
                        poly_id, item_id, dt, exc,
                        exc_info=True,
                    )
                    continue

            catalogue_rows.append(
                {
                    "polygon_id": poly_id,
                    "item_id": item_id,
                    "platform": platform,
                    "datetime": dt,
                    "cloud_cover": item["properties"].get("eo:cloud_cover", None),
                    "output_file": out_path,
                    **stats,
                }
            )

    csv_path = os.path.join(metadata_dir, "sentinel_catalogue.csv")
    fieldnames = [
        "polygon_id",
        "item_id",
        "platform",
        "datetime",
        "cloud_cover",
        "output_file",
        "valid_pixel_count",
        "mean_blue",
        "mean_green",
        "mean_red",
        "mean_nir",
        "blue_nir_ratio",
        "ndvi_mean",
        "ndvi_sd",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(catalogue_rows)

    log.info("Wrote catalogue -> %s", csv_path)
    log.info("All phases complete")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Fatal error in main()")
        raise