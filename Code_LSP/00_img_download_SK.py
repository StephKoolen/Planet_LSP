#!/usr/bin/env python3
# -*- coding: utf-8 -*-
PLANET_API_KEY = "PLAK145bb12f978a47be94c668cce23b6406"

"""
PlanetScope downloader -- Option A: global dedup + local clipping.

Pipeline
--------
Phase 1  SEARCH   -- Query Planet per polygon (parallel).
                     Build a global scene registry: scene_id -> feature.
                     Build a reverse lookup:        scene_id -> [polygon names].
                     Deduplication is implicit: scene IDs are dict keys.

Phase 2  ORDER    -- Chunk the deduplicated scene IDs.
                     Submit orders WITHOUT a clip tool (full tiles).
                     Download into a shared scene pool:
                         OUTPUT/scene_pool/<year>/<chunk_N>/data/

Phase 3  CLIP     -- For every scene in the pool, look up which polygons
                     it belongs to and clip with rasterio.mask.
                     Write clipped output to:
                         OUTPUT/<polygon_name>/<year>/

Output layout
-------------
OUTPUT/
+-scene_pool/                   shared unclipped tiles
|   +-metadata/
|   |   +-scene_registry.json      scene_id -> feature
|   |   +-polygon_membership.json  scene_id -> [polygon names]
|   |   +-order_result_chunk_N.json
|   +-<year>/
|       +-chunk_N/
|           +-data/
+-<polygon_name>/               clipped per-polygon outputs
    +-<year>/
        +-<scene_id>_clipped.tif

Dependencies
------------
    pip install requests geopandas shapely rasterio pyproj
"""

import os
import sys
import json
import time
import logging
import logging.handlers
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import geopandas as gpd
import rasterio
from rasterio.mask import mask as rio_mask
from shapely.geometry import mapping
from shapely.ops import orient
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ------------------------------------------------------------------
# USER SETTINGS
# ------------------------------------------------------------------

#PLANET_API_KEY = "your_API_key_here"

SHAPEFILE        = r"C:/Users/reub0539/OneDrive - Nexus365/Dphil/Projects/Project3/NFI_50ha/wytham_bagley.shp"
OUTPUT_DIR       = r"C:/Users/reub0539/work/Planet_LSP/data/wytham_test"
POLYGON_ID_FIELD = "forest"

MIN_YEAR = 2025
MAX_YEAR = 2025

MAX_CLOUD_COVER = 0.5
CHUNK_SIZE      = 400           # Planet hard limit is 500; 400 gives headroom

# Parallel workers for the search phase (one thread per polygon).
# Keep <= 4 to stay within Planet rate limits.
MAX_SEARCH_WORKERS = 4

# Parallel workers for the clip phase (CPU-bound, so match core count).
MAX_CLIP_WORKERS = 4

# Minutes before wait_for_order gives up on a stuck order.
ORDER_TIMEOUT_MINUTES = 120

BASE_URL   = "https://api.planet.com/data/v1"
QUICK_URL  = f"{BASE_URL}/quick-search"
ORDERS_URL = "https://api.planet.com/compute/ops/orders/v2"

# ------------------------------------------------------------------
# LOGGING  -- writes to both a rotating file and stdout with UTF-8
# ------------------------------------------------------------------

os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            os.path.join(OUTPUT_DIR, "planet_download.log"),
            maxBytes=10_000_000,
            backupCount=5,
            encoding="utf-8",   # explicit UTF-8 avoids cp1252 errors on Windows
        ),
        # Force UTF-8 on stdout so special characters don't crash on Windows.
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
    """
    Authenticated requests.Session with automatic retry on transient
    failures (5xx, 429) using exponential back-off.
    """
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
# PLANET API HELPERS
# ------------------------------------------------------------------

def create_filter(coords: list) -> dict:
    """
    AndFilter covering the full year range.  Seasonal (Mar-Jun) filtering
    is done in Python because the Planet API cannot express a recurring
    seasonal window across multiple calendar years.
    """
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
                # Only return orthorectified, radiometrically calibrated scenes.
                # Planet marks these as "standard"; non-standard scenes are
                # engineering/test acquisitions that are not orthorectified.
                "type": "StringInFilter",
                "field_name": "quality_category",
                "config": ["standard"],
            },
        ],
    }


def collect_features(sess: requests.Session, first_page: dict) -> list:
    """Exhaust all pagination pages and return a flat list of features."""
    features = []
    page = first_page
    while True:
        features.extend(page["features"])
        next_url = page["_links"].get("_next")
        if not next_url:
            break
        page = sess.get(next_url).json()
    return features


def is_spring(acquired: str) -> bool:
    """Return True if the ISO-8601 timestamp falls within March-June inclusive."""
    month = int(acquired[5:7])
    return 3 <= month <= 6


def chunk_list(items: list, size: int) -> list:
    return [items[i : i + size] for i in range(0, len(items), size)]


def submit_order(sess: requests.Session, order_request: dict) -> str:
    """POST an order and return its polling URL."""
    r = sess.post(ORDERS_URL, json=order_request)
    if r.status_code == 400:
        log.error(
            f"Order 400 Bad Request. "
            f"API response: {r.text}. "
            f"Request sent: {json.dumps(order_request, indent=2)}"
        )
    r.raise_for_status()
    order_id  = r.json()["id"]
    order_url = f"{ORDERS_URL}/{order_id}"
    log.info(f"Order submitted: {order_url}")
    return order_url


def wait_for_order(
    sess: requests.Session,
    order_url: str,
    timeout_minutes: int = ORDER_TIMEOUT_MINUTES,
) -> dict:
    """
    Poll every 30 s until the order reaches a terminal state.
    Raises TimeoutError if it has not completed within timeout_minutes.
    """
    deadline = time.monotonic() + timeout_minutes * 60
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Order did not complete within {timeout_minutes} min: {order_url}"
            )
        r = sess.get(order_url)
        r.raise_for_status()
        data  = r.json()
        state = data["state"]
        log.info(f"Order state: {state}  ({order_url})")
        if state in {"success", "failed", "partial"}:
            return data
        time.sleep(30)

def download_file(url: str, outfile: str, sess: requests.Session) -> None:
    if os.path.exists(outfile):
        return

    os.makedirs(os.path.dirname(outfile), exist_ok=True)

    r = sess.get(url, stream=True)
    r.raise_for_status()

    tmp = outfile + ".part"
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

    os.replace(tmp, outfile)


def download_results(results: list, outdir: str, sess: requests.Session) -> None:
    os.makedirs(outdir, exist_ok=True)

    for item in results[:10]:
        log.info(f"Result: {item['name']} -> {item['location'][:120]}...")

    tasks = [
        (item["location"], os.path.join(outdir, item["name"]))
        for item in results
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(download_file, url, path, sess) for url, path in tasks]
        for future in futures:
            future.result()
# ------------------------------------------------------------------
# GEOMETRY HELPER
# ------------------------------------------------------------------

def safe_coords(geom) -> list:
    """
    Extract coordinates from a shapely Polygon suitable for the Planet
    GeometryFilter.

    Three normalisation steps are applied:
    1. orient(sign=1.0) -- ensures the exterior ring is counter-clockwise,
       as required by the GeoJSON / Planet spec.  Shapely's mapping() does
       not guarantee winding order, which causes Planet to return 400.
    2. Strip Z coordinates -- Planet's API rejects 3D coordinate tuples.
       Shapefiles reprojected from a UTM CRS that includes elevation data
       produce (lon, lat, z) vertices; the Z must be dropped before sending.
    3. Validate that the result looks like WGS-84 (lon -180..180, lat -90..90).
       If it does not, the reprojection silently failed and we raise early.
    """
    geom = orient(geom, sign=1.0)
    coords = mapping(geom)["coordinates"]

    # Strip Z: each vertex may be (lon, lat, z) -- reduce to (lon, lat).
    coords = [[(v[0], v[1]) for v in ring] for ring in coords]

    # Spot-check the first vertex of the exterior ring.
    lon, lat = coords[0][0][0], coords[0][0][1]
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(
            f"Coordinates look wrong after reprojection: lon={lon}, lat={lat}. "
            "Check that the shapefile CRS is being detected correctly."
        )

    return coords

# ------------------------------------------------------------------
# PHASE 1 -- SEARCH
# ------------------------------------------------------------------

def search_polygon(sess: requests.Session, polygon_name: str, coords: list) -> list:
    """
    Search Planet for scenes intersecting one polygon and return only
    spring (Mar-Jun) scenes within the configured year range.

    On 400 errors the full API response body is logged to help diagnose
    filter or geometry issues.
    """
    search_request = {
        "filter": create_filter(coords),
        "item_types": ["PSScene"],
    }
    resp = sess.post(QUICK_URL, json=search_request)

    if resp.status_code == 400:
        # Log the response body -- it contains a human-readable reason
        # (e.g. "Invalid geometry", "Unknown field name") that is otherwise
        # swallowed by raise_for_status().
        log.error(
            f"{polygon_name}: 400 Bad Request. "
            f"API response: {resp.text}. "
            f"Request sent: {json.dumps(search_request, indent=2)}"
        )
    resp.raise_for_status()

    all_features = collect_features(sess, resp.json())
    spring_features = [
        f for f in all_features
        if is_spring(f["properties"]["acquired"])
        and MIN_YEAR <= int(f["properties"]["acquired"][:4]) <= MAX_YEAR
    ]

    log.info(
        f"{polygon_name}: {len(all_features)} scenes found, "
        f"{len(spring_features)} pass Mar-Jun filter"
    )
    return spring_features


def build_global_registry(
    sess: requests.Session,
    tasks: list,
) -> tuple[dict, dict]:
    """
    Run searches for all polygons concurrently and return:
        scene_registry    : scene_id -> feature dict
        polygon_membership: scene_id -> sorted list of polygon names
    """
    scene_registry     = {}
    polygon_membership = defaultdict(set)

    def _search(args):
        name, coords = args
        return name, search_polygon(sess, name, coords)

    with ThreadPoolExecutor(max_workers=MAX_SEARCH_WORKERS) as pool:
        futures = {pool.submit(_search, t): t[0] for t in tasks}
        for future in as_completed(futures):
            poly_name = futures[future]
            try:
                name, features = future.result()
                for feat in features:
                    sid = feat["id"]
                    scene_registry[sid] = feat
                    polygon_membership[sid].add(name)
            except Exception as exc:
                log.error(f"Search failed for {poly_name}: {exc}", exc_info=True)

    polygon_membership = {
        sid: sorted(polys) for sid, polys in polygon_membership.items()
    }
    log.info(
        f"Global registry: {len(scene_registry)} unique scenes across "
        f"{len(tasks)} polygons"
    )
    return scene_registry, polygon_membership

# ------------------------------------------------------------------
# PHASE 2 -- ORDER + DOWNLOAD
# ------------------------------------------------------------------

def run_global_orders(
    sess: requests.Session,
    scene_registry: dict,
    polygon_membership: dict,
    pool_dir: str,
) -> None:
    """
    Chunk all unique scene IDs, submit one order per chunk (no clip tool),
    and download full tiles into pool_dir.
    """
    metadata_dir = os.path.join(pool_dir, "metadata")
    os.makedirs(metadata_dir, exist_ok=True)

    with open(os.path.join(metadata_dir, "scene_registry.json"), "w") as f:
        json.dump(scene_registry, f, indent=2)
    with open(os.path.join(metadata_dir, "polygon_membership.json"), "w") as f:
        json.dump(polygon_membership, f, indent=2)

    by_year = defaultdict(list)
    for sid, feat in scene_registry.items():
        year = int(feat["properties"]["acquired"][:4])
        by_year[year].append(sid)

    for year, ids in sorted(by_year.items()):
        log.info(f"Pool year {year}: {len(ids)} unique scenes to order")
        chunks = chunk_list(ids, CHUNK_SIZE)

        for chunk_num, chunk in enumerate(chunks, start=1):
            completed_file = os.path.join(
                metadata_dir, f"order_result_{year}_chunk_{chunk_num}.json"
            )
            if os.path.exists(completed_file):
                log.info(f"Year {year} chunk {chunk_num}: already completed, skipping")
                continue

            order_name    = f"global_{year}_chunk_{chunk_num}"
            order_request = {
                "name": order_name,
                "order_type": "partial",
                "products": [
                    {
                        "item_ids": chunk,
                        "item_type": "PSScene",
                        # analytic_sr_udm2 provides surface reflectance + quality mask.
                        # ortho_analytic_4b_sr is the 4-band SR asset (R, G, B, NIR);
                        # bands 3 (Red) and 4 (NIR) are extracted locally for NDVI.
                        # analytic_sr_udm2 bundles ortho_analytic_4b_sr (SR bands)
                        # and ortho_udm2 (quality mask) -- no need to list
                        # asset_types separately; that field is not valid here
                        # and causes a 400. Band extraction to Red+NIR happens
                        # locally in Phase 3.
                        "product_bundle": "analytic_sr_udm2",
                    }
                ],
                # No clip tool -- full tiles are downloaded and clipped locally
                # in Phase 3, avoiding redundant Planet API calls for overlapping
                # polygons.
            }

            with open(
                os.path.join(metadata_dir, f"order_request_{year}_chunk_{chunk_num}.json"), "w"
            ) as f:
                json.dump(order_request, f, indent=2)

            order_url = submit_order(sess, order_request)
            try:
                response = wait_for_order(sess, order_url)
            except TimeoutError as exc:
                log.error(exc)
                continue

            with open(completed_file, "w") as f:
                json.dump(response, f, indent=2)

            if response["state"] == "failed":
                log.error(f"Order failed: {order_name}")
                continue

            data_dir = os.path.join(pool_dir, str(year), f"chunk_{chunk_num}", "data")
            download_results(response["_links"]["results"], data_dir, sess)
            log.info(f"Year {year} chunk {chunk_num}: download complete -> {data_dir}")

# ------------------------------------------------------------------
# PHASE 3 -- LOCAL CLIPPING
# ------------------------------------------------------------------

def clip_scene_pair_to_polygon(sr_path: Path, udm_path: Path, polygon_geom, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    from pyproj import Transformer
    from shapely.ops import transform as shp_transform

    def _clip_one(src_path: Path, out_path: str):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return

        with rasterio.open(src_path) as src:
            clip_geom = polygon_geom
            if src.crs and src.crs.to_epsg() != 4326:
                transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
                clip_geom = shp_transform(transformer.transform, clip_geom)

            clipped, transform = rio_mask(
                src,
                [mapping(clip_geom)],
                crop=True,
                all_touched=False,
            )

            profile = src.profile.copy()
            profile.update(
                count=clipped.shape[0],
                height=clipped.shape[1],
                width=clipped.shape[2],
                transform=transform,
            )

            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(clipped)

    sr_out = os.path.join(out_dir, f"{sr_path.stem}_4band.tif")
    udm_out = os.path.join(out_dir, f"{udm_path.stem}_udm2_clipped.tif")

    _clip_one(sr_path, sr_out)
    _clip_one(udm_path, udm_out)


def clip_all_scenes(
    pool_dir: str,
    polygon_membership: dict,
    polygon_geometries: dict,
    scene_registry: dict,
) -> None:
    pool_tifs = list(Path(pool_dir).rglob("*.tif"))

    if not pool_tifs:
        raise RuntimeError(f"No .tif files found under {pool_dir}.")

    clip_tasks = []

    for scene_id, poly_names in polygon_membership.items():
        year = int(scene_registry[scene_id]["properties"]["acquired"][:4])

        sr_files = [
            path for path in pool_tifs
            if path.name.endswith("_AnalyticMS_SR.tif") and scene_id in path.name
        ]
        udm_files = [
            path for path in pool_tifs
            if path.name.endswith("_udm2.tif") and scene_id in path.name
        ]

        if not sr_files:
            log.warning(f"No SR file found for scene {scene_id}")
            continue
        if not udm_files:
            log.warning(f"No UDM2 file found for scene {scene_id}")
            continue

        sr_path = sr_files[0]
        udm_path = udm_files[0]

        for poly_name in poly_names:
            geom = polygon_geometries.get(poly_name)
            if geom is None:
                continue

            out_dir = os.path.join(OUTPUT_DIR, poly_name, str(year))
            clip_tasks.append((sr_path, udm_path, geom, out_dir))

    log.info(f"Clip phase: {len(clip_tasks)} clip operations to run")

    def _clip(task):
        sr_path, udm_path, geom, out_dir = task
        try:
            clip_scene_pair_to_polygon(sr_path, udm_path, geom, out_dir)
        except Exception as exc:
            log.error(
                f"Clip failed: {sr_path} / {udm_path} -> {out_dir}: {exc}",
                exc_info=True,
            )
            raise

    with ThreadPoolExecutor(max_workers=MAX_CLIP_WORKERS) as pool:
        futures = [pool.submit(_clip, task) for task in clip_tasks]
        for future in futures:
            future.result()

    log.info("Clip phase complete")

# ------------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------------

def main() -> None:
    """
    Orchestrate all three phases.

    The scene_registry and polygon_membership produced in Phase 1 are
    persisted to disk so the script can be re-run from Phase 2 or 3
    without repeating API searches.
    """
    sess     = build_session()
    pool_dir = os.path.join(OUTPUT_DIR, "scene_pool")

    # ----------------------------------------------------------------
    # Load shapefile
    # ----------------------------------------------------------------

    gdf = gpd.read_file(SHAPEFILE)

    if gdf.crs is None:
        raise ValueError("Shapefile has no CRS defined")

    if gdf.crs.to_epsg() != 4326:
        log.info(f"Reprojecting from {gdf.crs} to EPSG:4326")
        gdf = gdf.to_crs(epsg=4326)

    # Warn early if the configured ID field is missing, then fall back to
    # the row index so the script can still run.
    if POLYGON_ID_FIELD not in gdf.columns:
        log.warning(
            f"Field '{POLYGON_ID_FIELD}' not found in shapefile. "
            f"Available fields: {gdf.columns.tolist()}. "
            f"Falling back to row index. Update POLYGON_ID_FIELD to fix this."
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
            try:
                coords = safe_coords(geom)
            except ValueError as exc:
                log.error(f"Skipping {name}: {exc}")
                continue
            search_tasks.append((name, coords))
            polygon_geometries[name] = geom

        elif geom.geom_type == "MultiPolygon":
            for i, poly in enumerate(geom.geoms, start=1):
                part_name = f"{name}_part{i}"
                try:
                    coords = safe_coords(poly)
                except ValueError as exc:
                    log.error(f"Skipping {part_name}: {exc}")
                    continue
                search_tasks.append((part_name, coords))
                polygon_geometries[part_name] = poly

        else:
            log.warning(f"Skipping unsupported geometry type '{geom.geom_type}' for {name}")

    log.info(f"Loaded {len(search_tasks)} polygon tasks from shapefile")

    # ----------------------------------------------------------------
    # Phase 1 -- Search  (resume: reload from disk if already done)
    # ----------------------------------------------------------------

    registry_path   = os.path.join(pool_dir, "metadata", "scene_registry.json")
    membership_path = os.path.join(pool_dir, "metadata", "polygon_membership.json")

    if os.path.exists(registry_path) and os.path.exists(membership_path):
        log.info("Phase 1: loading existing registry from disk (delete to re-search)")
        with open(registry_path)   as f: scene_registry     = json.load(f)
        with open(membership_path) as f: polygon_membership = json.load(f)
    else:
        log.info("Phase 1: searching Planet API for all polygons")
        scene_registry, polygon_membership = build_global_registry(sess, search_tasks)

    # ----------------------------------------------------------------
    # Phase 2 -- Order + download
    # ----------------------------------------------------------------

    log.info("Phase 2: submitting global orders and downloading scene pool")
    run_global_orders(sess, scene_registry, polygon_membership, pool_dir)

    # ----------------------------------------------------------------
    # Phase 3 -- Local clipping
    # ----------------------------------------------------------------

    log.info("Phase 3: clipping pool scenes to per-polygon outputs")
    clip_all_scenes(pool_dir, polygon_membership, polygon_geometries, scene_registry)

    log.info("All phases complete")


if __name__ == "__main__":
    main()
