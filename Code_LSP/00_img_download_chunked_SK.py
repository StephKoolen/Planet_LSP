"""
PlanetScope downloader -- Option A: global dedup + local clipping.

This version is designed for low disk usage:
- Phase 1 searches Planet per polygon and builds a global registry.
- Phase 2 submits one order chunk at a time.
- Each downloaded chunk is clipped immediately.
- After clipping, the temporary downloaded chunk is deleted.
- Final outputs are only the masked 4-band GeoTIFFs plus a CSV catalogue.

Important differences from the previously uploaded version:
1) main() is fully restored and actually orchestrates all phases.
2) Ordering, download, clipping, and cleanup now happen chunk-by-chunk.
3) The temporary scene_pool is deleted as each chunk is processed.
4) UDM2 band 1 controls masking; pixels where band 1 != 1 become -9999.
5) A CSV catalogue is written outside scene_pool for later QA/QC in R.

Dependencies
------------
    pip install requests geopandas shapely rasterio pyproj numpy
"""

import os
import sys
import json
import time
import csv
import shutil
import logging
import logging.handlers
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
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

PLANET_API_KEY = "PLAK145bb12f978a47be94c668cce23b6406"

SHAPEFILE = r"C:/Users/reub0539/OneDrive - Nexus365/Dphil/Projects/Project3/NFI_50ha/wytham_bagley.shp"
OUTPUT_DIR = r"C:/Users/reub0539/work/Planet_LSP/data/wytham_test"
OUTPUT_METADATA_DIR = os.path.join(OUTPUT_DIR, "metadata")
POLYGON_ID_FIELD = "forest"   # field in the shapefile that contains a unique name/ID for each polygon

MIN_YEAR = 2025
MAX_YEAR = 2025

MAX_CLOUD_COVER = 0.5
CHUNK_SIZE = 400               # Planet hard limit is 500; 400 gives headroom
MAX_SEARCH_WORKERS = 4
MAX_CLIP_WORKERS = 4
ORDER_TIMEOUT_MINUTES = 120

BASE_URL = "https://api.planet.com/data/v1"
QUICK_URL = f"{BASE_URL}/quick-search"
ORDERS_URL = "https://api.planet.com/compute/ops/orders/v2"

# Temporary scene pool is deleted after each chunk is processed.
DELETE_SCENE_POOL_INPUTS = True

# ------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_METADATA_DIR, exist_ok=True)

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
    """Authenticated requests.Session with retry on transient failures."""
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
    """Create the Planet search filter for one polygon."""
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
                    "lt": f"{MAX_YEAR + 1}-01-01T00:00:00.000Z",
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
    """Exhaust all pagination pages and return a flat list of features."""
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
    """Return True if the ISO-8601 timestamp falls within March-June inclusive."""
    month = int(acquired[5:7])
    return 3 <= month <= 6


def chunk_list(items: list, size: int) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)]


def submit_order(sess: requests.Session, order_request: dict) -> str:
    """POST an order and return its polling URL."""
    r = sess.post(ORDERS_URL, json=order_request)
    if r.status_code == 400:
        log.error(
            "Order 400 Bad Request. API response: %s. Request sent: %s",
            r.text,
            json.dumps(order_request, indent=2),
        )
    r.raise_for_status()
    order_id = r.json()["id"]
    order_url = f"{ORDERS_URL}/{order_id}"
    log.info(f"Order submitted: {order_url}")
    return order_url


def wait_for_order(
    sess: requests.Session,
    order_url: str,
    timeout_minutes: int = ORDER_TIMEOUT_MINUTES,
) -> dict:
    """Poll every 30 s until the order reaches a terminal state."""
    deadline = time.monotonic() + timeout_minutes * 60
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Order did not complete within {timeout_minutes} min: {order_url}"
            )
        r = sess.get(order_url)
        r.raise_for_status()
        data = r.json()
        state = data["state"]
        log.info(f"Order state: {state}  ({order_url})")
        if state in {"success", "failed", "partial"}:
            return data
        time.sleep(30)


def download_file(url: str, outfile: str, sess: requests.Session) -> None:
    """Download one file safely to a temporary file and rename it."""
    if os.path.exists(outfile) and os.path.getsize(outfile) > 0:
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
    """Download all order results and verify that they really landed locally."""
    os.makedirs(outdir, exist_ok=True)

    if not results:
        raise RuntimeError("Order returned zero results.")

    for item in results[:10]:
        log.info(f"Result: {item['name']} -> {item['location'][:120]}...")

    tasks = [
        (item["location"], os.path.join(outdir, item["name"]))
        for item in results
        if item.get("delivery") == "success"
    ]

    if not tasks:
        raise RuntimeError("Order results contained no successful deliveries.")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(download_file, url, path, sess) for url, path in tasks]
        for future in futures:
            future.result()

    downloaded = [path for _, path in tasks if os.path.exists(path) and os.path.getsize(path) > 0]
    log.info("Download verification: expected=%d, found=%d", len(tasks), len(downloaded))

    if len(downloaded) == 0:
        sample = [str(p) for p in Path(outdir).rglob("*")][:25]
        raise RuntimeError(
            "No files were written to the download directory. "
            f"Outdir={outdir}. Sample contents: {sample}"
        )

    if len(downloaded) < len(tasks):
        missing = [path for _, path in tasks if not (os.path.exists(path) and os.path.getsize(path) > 0)]
        raise RuntimeError(
            f"Only {len(downloaded)}/{len(tasks)} result files were downloaded successfully. "
            f"Missing: {missing[:10]}"
        )

# ------------------------------------------------------------------
# GEOMETRY HELPER
# ------------------------------------------------------------------

def safe_coords(geom) -> list:
    """Extract coordinates from a shapely Polygon suitable for the Planet GeometryFilter."""
    geom = orient(geom, sign=1.0)
    coords = mapping(geom)["coordinates"]

    # Strip Z: each vertex may be (lon, lat, z) -> (lon, lat)
    coords = [[(v[0], v[1]) for v in ring] for ring in coords]

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
    """Search Planet for scenes intersecting one polygon and return spring scenes."""
    search_request = {
        "filter": create_filter(coords),
        "item_types": ["PSScene"],
    }
    resp = sess.post(QUICK_URL, json=search_request)

    if resp.status_code == 400:
        log.error(
            f"{polygon_name}: 400 Bad Request. API response: {resp.text}. "
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


def build_global_registry(tasks: list) -> tuple[dict, dict]:
    """Run searches for all polygons concurrently and return scene registry + membership."""
    scene_registry = {}
    polygon_membership = defaultdict(set)

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
                    scene_registry[sid] = feat
                    polygon_membership[sid].add(name)
            except Exception as exc:
                log.error(f"Search failed for {poly_name}: {exc}", exc_info=True)

    polygon_membership = {sid: sorted(polys) for sid, polys in polygon_membership.items()}
    log.info(
        f"Global registry: {len(scene_registry)} unique scenes across {len(tasks)} polygons"
    )
    return scene_registry, polygon_membership

# ------------------------------------------------------------------
# PHASE 2 + 3 -- ORDER, DOWNLOAD, CLIP, CLEANUP
# ------------------------------------------------------------------


def _timestamp_from_sr_name(sr_path: Path) -> str:
    """Extract YYYYMMDD_HHMMSS from a Planet SR filename."""
    parts = sr_path.stem.split("_")
    if len(parts) < 2:
        raise ValueError(f"Unexpected SR filename format: {sr_path.name}")
    return "_".join(parts[:2])


def _delete_empty_parent_dirs(start_path: Path, stop_dir: Path) -> None:
    """Remove empty parent directories up to (but not including) stop_dir."""
    current = start_path.parent
    stop_dir = stop_dir.resolve()

    while current.exists():
        try:
            current.rmdir()
        except OSError:
            break

        if current.resolve() == stop_dir:
            break
        current = current.parent


def clip_scene_pair_to_polygon(
    sr_path: Path,
    udm_path: Path,
    polygon_geom,
    out_path: str,
) -> dict:
    """
    Clip SR + UDM2, mask SR pixels where UDM2 band 1 != 1, save final 4-band output.

    Returns a dictionary of summary statistics computed on the valid (unmasked)
    pixels in the final clipped SR image.
    """
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        log.info(f"Skipping existing output: {out_path}")
        # We do not recompute stats for pre-existing outputs here.
        return {
            "valid_pixel_count": None,
            "mean_blue": None,
            "mean_green": None,
            "mean_red": None,
            "mean_nir": None,
            "blue_nir_ratio": None,
            "ndvi_mean": None,
            "ndvi_sd": None,
        }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    from pyproj import Transformer
    from shapely.ops import transform as shp_transform

    with rasterio.open(sr_path) as sr_src, rasterio.open(udm_path) as udm_src:
        sr_geom = polygon_geom
        udm_geom = polygon_geom

        if sr_src.crs and sr_src.crs.to_epsg() != 4326:
            transformer_sr = Transformer.from_crs("EPSG:4326", sr_src.crs, always_xy=True)
            sr_geom = shp_transform(transformer_sr.transform, sr_geom)

        if udm_src.crs and udm_src.crs.to_epsg() != 4326:
            transformer_udm = Transformer.from_crs("EPSG:4326", udm_src.crs, always_xy=True)
            udm_geom = shp_transform(transformer_udm.transform, udm_geom)

        sr_clipped, sr_transform = rio_mask(
            sr_src,
            [mapping(sr_geom)],
            crop=True,
            all_touched=False,
        )

        udm_clipped, _ = rio_mask(
            udm_src,
            [mapping(udm_geom)],
            crop=True,
            all_touched=False,
        )

        if sr_clipped.shape[1:] != udm_clipped.shape[1:]:
            raise ValueError(
                f"Clipped SR and UDM2 shapes do not match for {sr_path.name}: "
                f"SR={sr_clipped.shape}, UDM2={udm_clipped.shape}"
            )

        # UDM2 band 1 (index 0) == 1 means clear / usable.
        clear_mask = udm_clipped[0] == 1

        # Convert SR to float so we can store nodata.
        masked_sr = sr_clipped.astype(np.float32)
        masked_sr[:, ~clear_mask] = -9999

        # Compute summary stats on valid pixels only.
        valid = masked_sr[0] != -9999
        valid_pixel_count = int(np.sum(valid))

        if valid_pixel_count == 0:
            mean_blue = mean_green = mean_red = mean_nir = None
            blue_nir_ratio = None
            ndvi_mean = None
            ndvi_sd = None
        else:
            blue = masked_sr[0][valid]
            green = masked_sr[1][valid]
            red = masked_sr[2][valid]
            nir = masked_sr[3][valid]

            mean_blue = float(np.mean(blue))
            mean_green = float(np.mean(green))
            mean_red = float(np.mean(red))
            mean_nir = float(np.mean(nir))

            blue_nir_ratio = float(mean_blue / mean_nir) if mean_nir and mean_nir > 0 else None

            ndvi = (nir - red) / (nir + red)
            ndvi_mean = float(np.mean(ndvi))
            ndvi_sd = float(np.std(ndvi))

        profile = sr_src.profile.copy()
        profile.update(
            count=masked_sr.shape[0],
            dtype="float32",
            nodata=-9999,
            height=masked_sr.shape[1],
            width=masked_sr.shape[2],
            transform=sr_transform,
            compress="deflate",
        )

        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(masked_sr)

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"Clip wrote no file: {out_path}")

    return {
        "valid_pixel_count": valid_pixel_count,
        "mean_blue": mean_blue,
        "mean_green": mean_green,
        "mean_red": mean_red,
        "mean_nir": mean_nir,
        "blue_nir_ratio": blue_nir_ratio,
        "ndvi_mean": ndvi_mean,
        "ndvi_sd": ndvi_sd,
    }

def process_downloaded_chunk(
    data_dir: str,
    scene_ids: list,
    scene_registry: dict,
    polygon_membership: dict,
    polygon_geometries: dict,
    catalogue_rows: list,
) -> None:
    """
    Clip all scenes in one downloaded chunk and collect catalogue rows.

    For each final output, this records:
    - polygon and scene IDs
    - source filenames
    - acquisition / satellite metadata
    - mean Blue, Green, Red, NIR
    - Blue/NIR ratio
    - mean NDVI and NDVI SD
    - valid pixel count
    """
    pool_tifs = list(Path(data_dir).rglob("*.tif"))

    log.info(f"Chunk clip: found {len(pool_tifs)} tif files under {data_dir}")
    for p in pool_tifs[:8]:
        log.info(f"Chunk file example: {p.name}")

    if not pool_tifs:
        raise RuntimeError(f"No .tif files found under {data_dir}.")

    clip_tasks = []

    for scene_id in scene_ids:
        poly_names = polygon_membership.get(scene_id, [])
        log.info(f"Trying scene_id: {scene_id} -> polygons: {poly_names}")

        sr_files = [
            path for path in pool_tifs
            if path.name.startswith(scene_id) and path.name.endswith("_AnalyticMS_SR.tif")
        ]
        udm_files = [
            path for path in pool_tifs
            if path.name.startswith(scene_id) and path.name.endswith("_udm2.tif")
        ]

        log.info(f"Matched {scene_id}: SR files={len(sr_files)}, UDM files={len(udm_files)}")

        if not sr_files:
            log.warning(f"No SR file found for scene {scene_id}")
            continue
        if not udm_files:
            log.warning(f"No UDM2 file found for scene {scene_id}")
            continue

        sr_path = sr_files[0]
        udm_path = udm_files[0]
        stamp = _timestamp_from_sr_name(sr_path)
        props = scene_registry.get(scene_id, {}).get("properties", {})

        log.info(f"Using SR:  {sr_path.name}")
        log.info(f"Using UDM: {udm_path.name}")

        for poly_name in poly_names:
            geom = polygon_geometries.get(poly_name)
            if geom is None:
                log.warning(f"No geometry found for polygon {poly_name}")
                continue

            out_path = os.path.join(
                OUTPUT_DIR,
                poly_name,
                f"a{poly_name}_{stamp}.tif",
            )

            clip_tasks.append(
                {
                    "scene_id": scene_id,
                    "polygon_id": poly_name,
                    "sr_path": sr_path,
                    "udm_path": udm_path,
                    "geom": geom,
                    "out_path": out_path,
                    "props": props,
                    "stamp": stamp,
                }
            )

    log.info(f"Chunk clip: built {len(clip_tasks)} tasks")

    if not clip_tasks:
        raise RuntimeError(
            "No clip tasks were created for this chunk. "
            "This usually means the scene IDs do not match the downloaded filenames."
        )

    def _clip(task):
        log.info(f"Clipping -> {task['out_path']}")
        stats = clip_scene_pair_to_polygon(
            task["sr_path"],
            task["udm_path"],
            task["geom"],
            task["out_path"],
        )

        row = {
            "polygon_id": task["polygon_id"],
            "scene_id": task["scene_id"],
            "output_file": task["out_path"],
            "source_sr_file": task["sr_path"].name,
            "source_udm_file": task["udm_path"].name,
            "acquired": task["props"].get("acquired", ""),
            "timestamp_yyyymmdd_hhmmss": task["stamp"],
            "satellite_id": task["props"].get("satellite_id", ""),
            "instrument": task["props"].get("instrument", ""),
            "quality_category": task["props"].get("quality_category", ""),
            "cloud_cover": task["props"].get("cloud_cover", ""),
            "clear_percent": task["props"].get("clear_percent", ""),
            "cloud_percent": task["props"].get("cloud_percent", ""),
            "shadow_percent": task["props"].get("shadow_percent", ""),
            "snow_ice_percent": task["props"].get("snow_ice_percent", ""),
            "view_angle": task["props"].get("view_angle", ""),
            "sun_elevation": task["props"].get("sun_elevation", ""),
            "sun_azimuth": task["props"].get("sun_azimuth", ""),
            "row_exists_before_run": False,
            "valid_pixel_count": stats["valid_pixel_count"],
            "mean_blue": stats["mean_blue"],
            "mean_green": stats["mean_green"],
            "mean_red": stats["mean_red"],
            "mean_nir": stats["mean_nir"],
            "blue_nir_ratio": stats["blue_nir_ratio"],
            "ndvi_mean": stats["ndvi_mean"],
            "ndvi_sd": stats["ndvi_sd"],
        }
        return row

    rows = []
    with ThreadPoolExecutor(max_workers=MAX_CLIP_WORKERS) as pool:
        futures = [pool.submit(_clip, task) for task in clip_tasks]
        for future in futures:
            rows.append(future.result())

    catalogue_rows.extend(rows)
    log.info("Chunk clipping complete")

def write_catalogue(catalogue_rows: list) -> None:
    """Write the metadata catalogue outside scene_pool."""
    catalogue_path = os.path.join(OUTPUT_METADATA_DIR, "clipped_scene_catalogue.csv")
    fieldnames = [
    "polygon_id",
    "scene_id",
    "output_file",
    "source_sr_file",
    "source_udm_file",
    "acquired",
    "timestamp_yyyymmdd_hhmmss",
    "satellite_id",
    "instrument",
    "quality_category",
    "cloud_cover",
    "clear_percent",
    "cloud_percent",
    "shadow_percent",
    "snow_ice_percent",
    "view_angle",
    "sun_elevation",
    "sun_azimuth",
    "row_exists_before_run",
    "valid_pixel_count",
    "mean_blue",
    "mean_green",
    "mean_red",
    "mean_nir",
    "blue_nir_ratio",
    "ndvi_mean",
    "ndvi_sd",
    ]

    with open(catalogue_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(catalogue_rows)

    log.info(f"Wrote output catalogue -> {catalogue_path}")


def run_global_orders_and_clip(
    sess: requests.Session,
    scene_registry: dict,
    polygon_membership: dict,
    polygon_geometries: dict,
    pool_dir: str,
    catalogue_rows: list,
) -> None:
    """Order one chunk at a time, clip immediately, then delete the temporary chunk."""
    by_year = defaultdict(list)
    for sid, feat in scene_registry.items():
        year = int(feat["properties"]["acquired"][:4])
        by_year[year].append(sid)

    for year, ids in sorted(by_year.items()):
        log.info(f"Pool year {year}: {len(ids)} unique scenes to order")
        chunks = chunk_list(ids, CHUNK_SIZE)

        for chunk_num, chunk in enumerate(chunks, start=1):
            log.info(f"Year {year} chunk {chunk_num}: preparing order for {len(chunk)} scenes")
            order_name = f"global_{year}_chunk_{chunk_num}"
            order_request = {
                "name": order_name,
                "order_type": "partial",
                "products": [
                    {
                        "item_ids": chunk,
                        "item_type": "PSScene",
                        "product_bundle": "analytic_sr_udm2",
                    }
                ],
            }

            order_url = submit_order(sess, order_request)
            try:
                response = wait_for_order(sess, order_url)
            except TimeoutError as exc:
                log.error(exc)
                continue

            if response.get("state") == "failed":
                log.error(f"Order failed: {order_name}")
                continue

            data_dir = os.path.join(pool_dir, str(year), f"chunk_{chunk_num}", "data")
            download_results(response["_links"]["results"], data_dir, sess)
            log.info(f"Year {year} chunk {chunk_num}: download complete -> {data_dir}")

            # Clip immediately from this chunk, then delete the temporary download.
            process_downloaded_chunk(
                data_dir=data_dir,
                scene_ids=chunk,
                scene_registry=scene_registry,
                polygon_membership=polygon_membership,
                polygon_geometries=polygon_geometries,
                catalogue_rows=catalogue_rows,
            )

            if DELETE_SCENE_POOL_INPUTS:
                chunk_root = Path(data_dir).parent
                try:
                    shutil.rmtree(chunk_root, ignore_errors=True)
                    log.info(f"Deleted temporary chunk directory: {chunk_root}")
                except Exception as exc:
                    log.warning(f"Could not delete chunk directory {chunk_root}: {exc}")

                _delete_empty_parent_dirs(chunk_root, Path(pool_dir))

    # Final cleanup of the temporary scene_pool root if it is now empty.
    pool_root = Path(pool_dir)
    if pool_root.exists():
        try:
            pool_root.rmdir()
            log.info(f"Deleted empty scene_pool root: {pool_root}")
        except OSError:
            log.info(f"scene_pool root not empty after cleanup: {pool_root}")

# ------------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------------

def main() -> None:
    """Orchestrate all phases."""
    sess = build_session()
    pool_dir = os.path.join(OUTPUT_DIR, "scene_pool")

    log.info(f"Using output directory: {OUTPUT_DIR}")
    log.info(f"Using metadata directory: {OUTPUT_METADATA_DIR}")
    log.info(f"Using temporary pool directory: {pool_dir}")

    # ----------------------------------------------------------------
    # Load shapefile
    # ----------------------------------------------------------------
    gdf = gpd.read_file(SHAPEFILE)

    if gdf.crs is None:
        raise ValueError("Shapefile has no CRS defined")

    if gdf.crs.to_epsg() != 4326:
        log.info(f"Reprojecting from {gdf.crs} to EPSG:4326")
        gdf = gdf.to_crs(epsg=4326)

    if POLYGON_ID_FIELD not in gdf.columns:
        log.warning(
            f"Field '{POLYGON_ID_FIELD}' not found in shapefile. "
            f"Available fields: {gdf.columns.tolist()}. "
            f"Falling back to row index. Update POLYGON_ID_FIELD to fix this."
        )

    search_tasks = []
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
    # Phase 1 -- Search (resume: reload from disk if already done)
    # ----------------------------------------------------------------
    registry_path = os.path.join(OUTPUT_METADATA_DIR, "scene_registry.json")
    membership_path = os.path.join(OUTPUT_METADATA_DIR, "polygon_membership.json")

    if os.path.exists(registry_path) and os.path.exists(membership_path):
        log.info("Phase 1: loading existing registry from disk (delete files to re-search)")
        with open(registry_path, encoding="utf-8") as f:
            scene_registry = json.load(f)
        with open(membership_path, encoding="utf-8") as f:
            polygon_membership = json.load(f)
    else:
        log.info("Phase 1: searching Planet API for all polygons")
        scene_registry, polygon_membership = build_global_registry(search_tasks)
        with open(registry_path, "w", encoding="utf-8") as f:
            json.dump(scene_registry, f, indent=2)
        with open(membership_path, "w", encoding="utf-8") as f:
            json.dump(polygon_membership, f, indent=2)

    # ----------------------------------------------------------------
    # Phase 2 + 3 -- Order chunk-by-chunk, clip immediately, cleanup
    # ----------------------------------------------------------------
    catalogue_rows = []
    log.info("Submitting orders one chunk at a time and clipping immediately")
    run_global_orders_and_clip(
        sess=sess,
        scene_registry=scene_registry,
        polygon_membership=polygon_membership,
        polygon_geometries=polygon_geometries,
        pool_dir=pool_dir,
        catalogue_rows=catalogue_rows,
    )

    write_catalogue(catalogue_rows)
    log.info("All phases complete")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Fatal error in main()")
        raise
