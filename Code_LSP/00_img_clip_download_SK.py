"""
PlanetScope downloader -- global dedup + Planet-side chunk clip + local polygon clip.

What this script does
---------------------
Phase 1  SEARCH
    - Query Planet per polygon (parallel)
    - Build a global scene registry: scene_id -> feature
    - Build a reverse lookup:        scene_id -> [polygon names]

Phase 2  ORDER + DOWNLOAD
    - Bucket deduplicated scene IDs by year (strict — no chunk ever spans two years)
    - Sort scene IDs within each year for deterministic chunking
    - Build one AOI per chunk from all polygons touched by the scenes in it
    - Submit Planet orders with the clip tool
    - Download the clipped chunk outputs into per-year folders:
        <OUTPUT_DIR>/<polygon_name>/downloaded/<year>/chunk_<N>/

Phase 3  LOCAL POLYGON CLIP
    - For each clipped chunk, clip each scene to the individual polygons it intersects
    - Reproject UDM2 to match SR CRS before clipping to avoid shape mismatches
    - Apply UDM2 band 1 (clear mask) to set bad pixels to -9999 in all 4 bands
    - Save final per-polygon outputs as:
        a[polygon_id]_YYYYMMDD_HHMMSS.tif

Disk usage
----------
The temporary chunk downloads can be deleted automatically after each chunk
is processed. Toggle DELETE_TEMP_DOWNLOADS below.

Dependencies
------------
    pip install requests geopandas shapely rasterio pyproj numpy
"""



import csv
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
#from __future__ import annotations
from itertools import groupby
from pathlib import Path

import geopandas as gpd
import numpy as np
import requests
import rasterio
from rasterio.enums import Resampling
from rasterio.mask import mask as rio_mask
from rasterio.warp import reproject
from requests.adapters import HTTPAdapter
from shapely.geometry import mapping
from shapely.ops import orient, transform as shp_transform, unary_union
from urllib3.util.retry import Retry

# ------------------------------------------------------------------
# USER SETTINGS
# ------------------------------------------------------------------

SHAPEFILE        = r"C:/Users/reub0539/OneDrive - Nexus365/Dphil/Projects/Project3/simplified_wytham.shp"
OUTPUT_DIR       = r"C:/Users/reub0539/work/Planet_LSP/data/wytham_test"
POLYGON_ID_FIELD = "forest"

MIN_YEAR = 2018
MAX_YEAR = 2025

MAX_CLOUD_COVER       = 0.5
CHUNK_SIZE            = 400
MAX_SEARCH_WORKERS    = 8
MAX_DOWNLOAD_WORKERS  = 12
MAX_CLIP_WORKERS      = 8
ORDER_TIMEOUT_MINUTES = 120

DELETE_TEMP_DOWNLOADS = False

BASE_URL   = "https://api.planet.com/data/v1"
QUICK_URL  = f"{BASE_URL}/quick-search"
ORDERS_URL = "https://api.planet.com/compute/ops/orders/v2"

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(os.path.join(OUTPUT_DIR, "metadata"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            os.path.join(OUTPUT_DIR, "planet_download.log"),
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
# SESSION
# ------------------------------------------------------------------

def build_session() -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    sess = requests.Session()
    sess.mount("https://", HTTPAdapter(max_retries=retry))
    sess.auth = (PLANET_API_KEY, "")
    return sess


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------

def create_filter(coords: list) -> dict:
    return {
        "type": "AndFilter",
        "config": [
            {
                "type": "GeometryFilter",
                "field_name": "geometry",
                "config": {"type": "Polygon", "coordinates": coords},
            },
            {
                "type": "DateRangeFilter",
                "field_name": "acquired",
                "config": {
                    "gte": f"{MIN_YEAR}-01-01T00:00:00.000Z",
                    "lt":  f"{MAX_YEAR + 1}-01-01T00:00:00.000Z",
                },
            },
            {
                "type": "RangeFilter",
                "field_name": "cloud_cover",
                "config": {"gte": 0, "lte": MAX_CLOUD_COVER},
            },
            {
                "type": "PermissionFilter",
                "config": ["assets:download"],
            },
            {
                "type": "StringInFilter",
                "field_name": "quality_category",
                "config": ["standard"],
            },
        ],
    }

def collect_features(sess: requests.Session, first_page: dict) -> list:
    features = []
    page = first_page
    while True:
        features.extend(page.get("features", []))
        next_url = page.get("_links", {}).get("_next")
        if not next_url:
            break
        r = sess.get(next_url)
        r.raise_for_status()
        page = r.json()
    return features

def is_spring(acquired: str) -> bool:
    month = int(acquired[5:7])
    return 3 <= month <= 6

def safe_coords(geom) -> list:
    geom   = orient(geom, sign=1.0)
    coords = mapping(geom)["coordinates"]
    coords = [[(v[0], v[1]) for v in ring] for ring in coords]
    lon, lat = coords[0][0][0], coords[0][0][1]
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(f"Coordinates look wrong after reprojection: lon={lon}, lat={lat}.")
    return coords

def chunk_list(items: list, size: int) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)]

def submit_order(sess: requests.Session, order_request: dict) -> str:
    r = sess.post(ORDERS_URL, json=order_request)

    if r.status_code >= 400:
        log.error("Order failed with %s", r.status_code)
        log.error("Response text: %s", r.text)
        log.error("Request sent: %s", json.dumps(order_request, indent=2))

    r.raise_for_status()
    order_id = r.json()["id"]
    order_url = f"{ORDERS_URL}/{order_id}"
    log.info("Order submitted: %s", order_url)
    return order_url

def wait_for_order(
    sess: requests.Session,
    order_url: str,
    timeout_minutes: int = ORDER_TIMEOUT_MINUTES,
) -> dict:
    deadline = time.monotonic() + timeout_minutes * 60
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Order did not complete within {timeout_minutes} min: {order_url}"
            )
        r     = sess.get(order_url)
        r.raise_for_status()
        data  = r.json()
        state = data.get("state")
        log.info("Order state: %s (%s)", state, order_url)
        if state in {"success", "failed", "partial"}:
            return data
        time.sleep(30)

def download_file(url: str, outfile: str, sess: requests.Session) -> None:
    if os.path.exists(outfile) and os.path.getsize(outfile) > 0:
        return
    os.makedirs(os.path.dirname(outfile), exist_ok=True)
    r   = sess.get(url, stream=True)
    r.raise_for_status()
    tmp = outfile + ".part"
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    os.replace(tmp, outfile)

def download_results(results: list, outdir: str, sess: requests.Session) -> list[str]:
    os.makedirs(outdir, exist_ok=True)
    if not results:
        raise RuntimeError("Order returned zero results.")

    tasks = [
        (item["location"], os.path.join(outdir, item["name"]))
        for item in results
        if item.get("delivery") == "success"
    ]
    if not tasks:
        raise RuntimeError("Order results contained no successful deliveries.")

    with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as pool:
        futures = [pool.submit(download_file, url, path, sess) for url, path in tasks]
        for future in futures:
            future.result()

    downloaded = [
        path for _, path in tasks
        if os.path.exists(path) and os.path.getsize(path) > 0
    ]
    log.info("Downloaded %d/%d files -> %s", len(downloaded), len(tasks), outdir)
    return downloaded


# ------------------------------------------------------------------
# PHASE 1 -- SEARCH
# ------------------------------------------------------------------

def search_polygon(sess: requests.Session, polygon_name: str, coords: list) -> list:
    search_request = {"filter": create_filter(coords), "item_types": ["PSScene"]}
    resp = sess.post(QUICK_URL, json=search_request)

    if resp.status_code == 400:
        log.error(
            "%s: 400 Bad Request. API response: %s. Request sent: %s",
            polygon_name,
            resp.text,
            json.dumps(search_request, indent=2),
        )
    resp.raise_for_status()

    all_features    = collect_features(sess, resp.json())
    spring_features = [
        f for f in all_features
        if is_spring(f["properties"]["acquired"])
        and MIN_YEAR <= int(f["properties"]["acquired"][:4]) <= MAX_YEAR
    ]

    log.info(
        "%s: %d scenes found, %d pass Mar-Jun filter",
        polygon_name, len(all_features), len(spring_features),
    )
    return spring_features

def build_polygon_registry(tasks: list) -> tuple[dict, dict]:
    """
    Search each polygon independently and return:
      scene_registry     : scene_id -> feature
      polygon_membership : polygon_name -> [scene_id]
    """
    scene_registry     = {}
    polygon_membership = defaultdict(list)

    def _search(args):
        name, coords = args
        with build_session() as sess_local:
            return name, search_polygon(sess_local, name, coords)

    with ThreadPoolExecutor(max_workers=MAX_SEARCH_WORKERS) as pool:
        futures = {pool.submit(_search, t): t[0] for t in tasks}
        for future in as_completed(futures):
            poly_name = futures[future]
            try:
                name, features = future.result()
                for feat in features:
                    sid = feat["id"]
                    scene_registry[sid]  = feat
                    polygon_membership[name].append(sid)
            except Exception as exc:
                log.error("Search failed for %s: %s", poly_name, exc, exc_info=True)

    # Deduplicate scene IDs within each polygon; sorting is done per-year below.
    for poly_name in list(polygon_membership.keys()):
        polygon_membership[poly_name] = sorted(set(polygon_membership[poly_name]))

    log.info(
        "Built registry with %d unique scenes across %d polygons",
        len(scene_registry), len(polygon_membership),
    )
    return scene_registry, polygon_membership


# ------------------------------------------------------------------
# PHASE 2 -- ORDER PER POLYGON, BUCKETED BY YEAR
# ------------------------------------------------------------------

def polygon_clip_aoi(coords: list) -> dict:
    """Use the polygon's own geometry as the AOI for the Planet clip tool."""
    return {"type": "Polygon", "coordinates": coords}


def bucket_scenes_by_year(scene_ids: list, scene_registry: dict) -> dict[str, list[str]]:
    """
    Group a list of scene IDs by their acquisition year.

    Returns a dict mapping year string -> sorted list of scene IDs.
    Scenes are sorted within each year for deterministic chunking.
    This guarantees that no chunk ever straddles two calendar years,
    so each chunk's download folder can be unambiguously named by year.
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for sid in scene_ids:
        # Acquisition timestamp format: "2023-04-15T10:22:00.000Z"
        # Slice [:4] gives the four-digit year string.
        year = scene_registry[sid]["properties"]["acquired"][:4]
        buckets[year].append(sid)

    # Sort scene IDs within each year for deterministic, reproducible chunking.
    return {year: sorted(sids) for year, sids in sorted(buckets.items())}


def run_orders_per_polygon(
    sess: requests.Session,
    scene_registry: dict,
    polygon_membership: dict,
    polygon_geometries: dict,
) -> None:
    """
    For each polygon:
      1. Bucket its scene IDs by year (strict — no chunk spans two years).
      2. Split each year's scenes into chunks of at most CHUNK_SIZE.
      3. Submit one Planet order per chunk, clipped to the polygon's AOI.
      4. Download results into:
             <OUTPUT_DIR>/<polygon_name>/downloaded/<year>/chunk_<N>/

    Output structure example:
        wytham/
        └── downloaded/
            ├── 2019/
            │   ├── chunk_1/
            │   └── chunk_2/
            ├── 2020/
            │   └── chunk_1/
            └── 2023/
                └── chunk_1/
    """
    metadata_dir = os.path.join(OUTPUT_DIR, "metadata")
    os.makedirs(metadata_dir, exist_ok=True)

    # Persist registry and membership for debugging / resuming.
    with open(os.path.join(metadata_dir, "scene_registry.json"), "w", encoding="utf-8") as f:
        json.dump(scene_registry, f, indent=2)
    with open(os.path.join(metadata_dir, "polygon_membership.json"), "w", encoding="utf-8") as f:
        json.dump(polygon_membership, f, indent=2)

    for poly_name, scene_ids in polygon_membership.items():

        geom = polygon_geometries.get(poly_name)
        if geom is None:
            log.warning("Skipping polygon %s because geometry is missing", poly_name)
            continue

        coords = safe_coords(geom)
        aoi    = polygon_clip_aoi(coords)

        # ── Bucket scenes by year ────────────────────────────────────────────
        # Each year gets its own set of chunks so downloads are always
        # organised into per-year folders with no ambiguity.
        year_buckets = bucket_scenes_by_year(scene_ids, scene_registry)

        total_scenes = sum(len(v) for v in year_buckets.values())
        log.info(
            "Polygon %s: %d scenes across %d years",
            poly_name, total_scenes, len(year_buckets),
        )

        # ── Process each year independently ──────────────────────────────────
        for year, year_scene_ids in year_buckets.items():

            chunks = chunk_list(year_scene_ids, CHUNK_SIZE)
            log.info(
                "  %s | year %s: %d scenes -> %d chunk(s)",
                poly_name, year, len(year_scene_ids), len(chunks),
            )

            for chunk_num, chunk in enumerate(chunks, start=1):

                order_name = f"{poly_name}_{year}_chunk_{chunk_num}"
                order_request = {
                    "name": order_name,
                    "source_type": "scenes",
                    "order_type": "partial",
                    "products": [
                        {
                            "item_ids": chunk,
                            "item_type": "PSScene",
                            "product_bundle": "analytic_sr_udm2,analytic_udm2",
                        }
                    ],
                    "tools": [
                        {"clip": {"aoi": aoi}}
                    ],
                }

                order_url = submit_order(sess, order_request)
                response  = wait_for_order(sess, order_url)

                if response.get("state") == "failed":
                    log.error(
                        "Order failed for %s year %s chunk %d",
                        poly_name, year, chunk_num,
                    )
                    continue

                # Download into  <poly>/downloaded/<year>/chunk_<N>/
                outdir = os.path.join(
                    OUTPUT_DIR, poly_name, "downloaded", year, f"chunk_{chunk_num}"
                )
                download_results(response["_links"]["results"], outdir, sess)

                if DELETE_TEMP_DOWNLOADS:
                    shutil.rmtree(outdir, ignore_errors=True)
                    log.info("Deleted temp download dir: %s", outdir)


# ------------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------------

def main() -> None:
    gdf = gpd.read_file(SHAPEFILE)
    if gdf.crs is None:
        raise ValueError("Shapefile has no CRS defined")

    if gdf.crs.to_epsg() != 4326:
        log.info("Reprojecting from %s to EPSG:4326", gdf.crs)
        gdf = gdf.to_crs(epsg=4326)

    if POLYGON_ID_FIELD not in gdf.columns:
        log.warning(
            "Field '%s' not found in shapefile. Available fields: %s. Falling back to row index.",
            POLYGON_ID_FIELD,
            gdf.columns.tolist(),
        )

    search_tasks       = []
    polygon_geometries = {}

    for idx, row in gdf.iterrows():
        name = (
            str(row[POLYGON_ID_FIELD])
            if POLYGON_ID_FIELD in gdf.columns
            else f"polygon_{idx:04d}"
        )
        geom = row.geometry

        if geom.geom_type == "Polygon":
            search_tasks.append((name, safe_coords(geom)))
            polygon_geometries[name] = geom

        elif geom.geom_type == "MultiPolygon":
            for i, poly in enumerate(geom.geoms, start=1):
                part_name = f"{name}_part{i}"
                search_tasks.append((part_name, safe_coords(poly)))
                polygon_geometries[part_name] = poly

        else:
            log.warning(
                "Skipping unsupported geometry type '%s' for %s",
                geom.geom_type, name,
            )

    log.info("Loaded %d polygon tasks from shapefile", len(search_tasks))

    with build_session() as sess:
        scene_registry, polygon_membership = build_polygon_registry(search_tasks)
        run_orders_per_polygon(sess, scene_registry, polygon_membership, polygon_geometries)

    log.info("All phases complete")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Fatal error in main()")
        raise