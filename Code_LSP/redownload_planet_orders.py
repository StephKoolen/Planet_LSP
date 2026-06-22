#!/usr/bin/env python3
"""Download files from one or more existing Planet orders.

This script does NOT create any new orders. It fetches existing orders by ID
(or URL), checks that they are complete, and downloads the order results into
local folders.

Usage examples:
  python redownload_planet_orders.py --order-id <ORDER_ID> --outdir C:\\data\\planet
  python redownload_planet_orders.py --order-id <ID1> --order-id <ID2> --outdir C:\\data\\planet
  python redownload_planet_orders.py --orders-file orders.txt --outdir C:\\data\\planet
  python redownload_planet_orders.py --log-file planet_download.log --outdir C:\\data\\planet

Authentication:
  Set the environment variable PLANET_API_KEY before running, or pass
  --api-key on the command line.
"""




#from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ORDERS_URL = "https://api.planet.com/compute/ops/orders/v2"
DEFAULT_POOL_MAXSIZE = 32
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def build_session(api_key: str) -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    sess = requests.Session()
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=DEFAULT_POOL_MAXSIZE,
        pool_maxsize=DEFAULT_POOL_MAXSIZE,
    )
    sess.mount("https://", adapter)
    sess.auth = (api_key, "")
    return sess


def normalize_order_id(order_id_or_url: str) -> str:
    s = order_id_or_url.strip()
    if not s:
        return s
    if s.startswith(ORDERS_URL):
        return s.rsplit("/", 1)[-1]
    m = UUID_RE.search(s)
    if m:
        return m.group(0)
    return s


def collect_order_ids(args: argparse.Namespace) -> list[str]:
    order_ids: list[str] = []

    for value in args.order_ids or []:
        order_ids.append(normalize_order_id(value))

    for value in args.order_urls or []:
        order_ids.append(normalize_order_id(value))

    if args.orders_file:
        with open(args.orders_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                order_ids.append(normalize_order_id(line))

    if args.log_file:
        with open(args.log_file, "r", encoding="utf-8") as f:
            for line in f:
                for match in UUID_RE.finditer(line):
                    order_ids.append(match.group(0))

    seen = set()
    deduped: list[str] = []
    for oid in order_ids:
        if oid and oid not in seen:
            deduped.append(oid)
            seen.add(oid)
    return deduped


def safe_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if ch in bad else ch for ch in name)
    out = out.strip().strip(".")
    return out or "planet_order"


def get_order(sess: requests.Session, order_id: str) -> dict:
    url = f"{ORDERS_URL}/{order_id}"
    r = sess.get(url)
    if r.status_code >= 400:
        raise RuntimeError(f"Failed to fetch order {order_id}: {r.status_code} {r.text}")
    return r.json()


def wait_for_order(sess: requests.Session, order_id: str, timeout_minutes: int = 120) -> dict:
    deadline = time.monotonic() + timeout_minutes * 60
    while True:
        order = get_order(sess, order_id)
        state = order.get("state")
        print(f"Order {order_id}: {state}")
        if state in {"success", "partial", "failed", "cancelled"}:
            return order
        if time.monotonic() > deadline:
            raise TimeoutError(f"Order did not complete within {timeout_minutes} minutes: {order_id}")
        time.sleep(30)


def results_from_order(order: dict) -> list[dict]:
    links = order.get("_links", {})
    results = links.get("results", []) or []
    if not results:
        raise RuntimeError("Order has no downloadable results.")
    return results


def download_file(sess: requests.Session, url: str, outpath: Path) -> Path:
    outpath.parent.mkdir(parents=True, exist_ok=True)
    if outpath.exists() and outpath.stat().st_size > 0:
        return outpath

    tmp = outpath.with_suffix(outpath.suffix + ".part")
    with sess.get(url, stream=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    os.replace(tmp, outpath)
    return outpath


def download_results(sess: requests.Session, results: Iterable[dict], outdir: Path, max_workers: int = 12) -> list[Path]:
    tasks: list[tuple[str, Path]] = []
    for item in results:
        url = item.get("location")
        name = item.get("name") or "download.bin"
        if not url:
            continue
        tasks.append((url, outdir / name))

    if not tasks:
        raise RuntimeError("No result URLs were found in the order response.")

    downloaded: list[Path] = []
    workers = min(max_workers, max(1, len(tasks)))
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(download_file, sess, url, path) for url, path in tasks]
        for fut in cf.as_completed(futs):
            downloaded.append(fut.result())

    return sorted(downloaded)


def extract_zip_files(files: Iterable[Path]) -> None:
    for fp in files:
        if fp.suffix.lower() != ".zip":
            continue
        extract_dir = fp.with_suffix("")
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(fp) as zf:
            zf.extractall(extract_dir)


def process_order(sess: requests.Session, order_id: str, outdir: Path, wait: bool, timeout_minutes: int, extract_zips: bool) -> bool:
    order = wait_for_order(sess, order_id, timeout_minutes=timeout_minutes) if wait else get_order(sess, order_id)
    state = order.get("state")
    if state not in {"success", "partial"}:
        print(f"Skipping {order_id}: order state is {state}", file=sys.stderr)
        return False

    order_name = safe_name(order.get("name") or order_id)
    order_dir = outdir / order_name
    order_dir.mkdir(parents=True, exist_ok=True)

    with open(order_dir / f"{order_name}_order.json", "w", encoding="utf-8") as f:
        json.dump(order, f, indent=2)

    results = results_from_order(order)
    files = download_results(sess, results, order_dir)

    if extract_zips:
        extract_zip_files(files)

    print(f"Downloaded {len(files)} file(s) for {order_id} -> {order_dir}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Redownload files from one or more existing Planet orders.")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--order-id", action="append", dest="order_ids", help="Planet order ID (repeatable)")
    group.add_argument("--order-url", action="append", dest="order_urls", help="Planet order URL (repeatable)")
    group.add_argument("--orders-file", help="Text file containing one order ID or URL per line")
    group.add_argument("--log-file", help="Planet log file to scan for order IDs")
    parser.add_argument("--outdir", required=True, help="Directory to save the downloaded files")
    parser.add_argument("--api-key", help="Planet API key (defaults to PLANET_API_KEY env var)")
    parser.add_argument("--wait", action="store_true", help="Wait until each order reaches a final state")
    parser.add_argument("--timeout-minutes", type=int, default=120, help="Maximum wait time when using --wait")
    parser.add_argument("--extract-zips", action="store_true", help="Extract ZIP results after download")
    parser.add_argument("--max-workers", type=int, default=12, help="Max download workers per order")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("PLANET_API_KEY")
    if not api_key:
        print("Missing API key. Set PLANET_API_KEY or pass --api-key.", file=sys.stderr)
        return 2

    order_ids = collect_order_ids(args)
    if not order_ids:
        print("No order IDs were provided. Use --order-id, --orders-file, or --log-file.", file=sys.stderr)
        return 2

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    sess = build_session(api_key)

    ok = 0
    failed = 0
    for order_id in order_ids:
        try:
            if process_order(
                sess,
                order_id,
                outdir,
                wait=args.wait,
                timeout_minutes=args.timeout_minutes,
                extract_zips=args.extract_zips,
            ):
                ok += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            print(f"Failed for {order_id}: {exc}", file=sys.stderr)

    print(f"Finished. Success: {ok}, failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
