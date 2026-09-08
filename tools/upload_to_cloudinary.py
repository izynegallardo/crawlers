"""
    Bulk-uploads a local directory of images to Cloudinary, preserving the
    original filename (minus extension) as the public_id - no random
    "_qgduox"-style suffix like Cloudinary's default upload behavior adds.

    Reads credentials from the CLOUDINARY_URL environment variable
    (cloudinary://<api_key>:<api_secret>@<cloud_name> - copy it straight
    from your Cloudinary dashboard's "API Environment variable" field). If
    python-dotenv is installed and a .env file exists in the repo root (or
    wherever this is run from), CLOUDINARY_URL can live there instead of
    needing to be set with $env: every new terminal session. Importing the
    cloudinary package reads whatever's in the environment automatically -
    nothing else to configure, no need to call cloudinary.config() yourself.

    Resumable against Cloudinary itself, not just a local file: before
    uploading anything, this lists what's already on Cloudinary (under
    --folder, if given) via the Admin API and treats that listing as the
    source of truth. Anything already there gets its URL backfilled into
    --mapping-csv without re-uploading, even if that mapping CSV is empty,
    lost, or was never written by this tool in the first place (e.g. a
    manual upload, or a previous run before --folder existed). The mapping
    CSV itself (common.csv_store.ResumableCsvStore) still skips anything
    it already knows about too, so a normal interrupted-and-resumed run
    doesn't even need the Cloudinary listing call to skip correctly - the
    listing is what makes this correct even when the local mapping isn't
    trustworthy on its own.

    This only uploads and records URLs - it does NOT touch your main
    dataset CSV. Once this finishes, run animepahe/apply_image_urls.py
    with the same --mapping-csv to replace image_url in the dataset with
    the Cloudinary URL.

    Usage (PowerShell):
        $env:CLOUDINARY_URL = "cloudinary://<api_key>:<api_secret>@<cloud_name>"
        python tools/upload_to_cloudinary.py animepahe/data/images --mapping-csv animepahe/data/cloudinary.csv --folder animepahe/posters

    Run from the repo root so `common` is importable.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    # Must run BEFORE `import cloudinary` below - cloudinary reads
    # CLOUDINARY_URL from the environment at import time (see its own
    # Config.__init__), so loading a .env file any later wouldn't take
    # effect. Optional: falls through to whatever's already in the real
    # environment if python-dotenv isn't installed or there's no .env file.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import cloudinary  # noqa: E402
import cloudinary.api  # noqa: E402
import cloudinary.uploader  # noqa: E402

from common.csv_store import ResumableCsvStore  # noqa: E402


def _check_credentials() -> None:
    config = cloudinary.config()
    if not (config.cloud_name and config.api_key and config.api_secret):
        raise SystemExit(
            "Cloudinary isn't configured. Set the CLOUDINARY_URL environment variable "
            "(cloudinary://<api_key>:<api_secret>@<cloud_name>, copy it from your "
            "Cloudinary dashboard) before running this."
        )


def _iter_image_files(source_dir: Path):
    for path in sorted(source_dir.iterdir()):
        if path.is_file() and not path.name.startswith("."):
            yield path


def _public_id_for(stem: str, folder: str | None) -> str:
    return f"{folder}/{stem}" if folder else stem


def _list_existing_on_cloudinary(folder: str | None) -> dict[str, str]:
    """
        {public_id: secure_url} for everything already on Cloudinary under
        folder (or the whole account root if folder is None). One Admin
        API call per 500 assets (Cloudinary's max page size), paginated via
        next_cursor - cheap compared to checking per-file, and it's the
        actual ground truth rather than trusting a local file to be
        complete and accurate.
    """
    existing: dict[str, str] = {}
    options = {"resource_type": "image", "type": "upload", "max_results": 500}
    if folder:
        options["prefix"] = folder.rstrip("/") + "/"

    next_cursor = None
    while True:
        if next_cursor:
            options["next_cursor"] = next_cursor
        result = cloudinary.api.resources(**options)
        for resource in result.get("resources", []):
            existing[resource["public_id"]] = resource["secure_url"]
        next_cursor = result.get("next_cursor")
        if not next_cursor:
            break

    return existing


async def _upload_one(path: Path, folder: str | None) -> dict:
    """
        Explicit public_id=path.stem (rather than use_filename=True) so the
        result is exactly the original filename, no Cloudinary-side
        normalization to worry about - see this file's docstring and
        https://support.cloudinary.com/hc/en-us/articles/202520762 for why
        unique_filename=False is also needed (use_filename alone still
        appends random characters to guarantee uniqueness).

        overwrite=False: by the time this is called, run() has already
        confirmed via the Cloudinary listing that this public_id doesn't
        exist yet, so overwrite is not expected to trigger - it's a safety
        default, not load-bearing logic.
    """
    options = {
        "public_id": path.stem,
        "unique_filename": False,
        "overwrite": False,
        "resource_type": "image",
    }
    if folder:
        options["folder"] = folder

    def _do_upload():
        return cloudinary.uploader.upload(str(path), **options)

    return await asyncio.to_thread(_do_upload)


async def run(args) -> None:
    source_dir = Path(args.source_dir)
    if not source_dir.is_dir():
        raise SystemExit(f"{source_dir} is not a directory")

    mapping = ResumableCsvStore(args.mapping_csv, ["pahe_id", "cloudinary_url"], id_field="pahe_id")
    already_in_mapping = mapping.count()

    print("Checking what's already on Cloudinary" + (f" under {args.folder}" if args.folder else "") + "...")
    existing_on_cloudinary = _list_existing_on_cloudinary(args.folder)
    print(f"{len(existing_on_cloudinary)} asset(s) found on Cloudinary")

    files = []
    backfilled = 0
    for path in _iter_image_files(source_dir):
        if mapping.exists(path.stem):
            continue

        public_id = _public_id_for(path.stem, args.folder)
        secure_url = existing_on_cloudinary.get(public_id)
        if secure_url:
            mapping.append({"pahe_id": path.stem, "cloudinary_url": secure_url})
            backfilled += 1
            continue

        files.append(path)

    print(
        f"{already_in_mapping} already in {args.mapping_csv}, "
        f"{backfilled} found on Cloudinary and backfilled into it, "
        f"{len(files)} to actually upload"
    )

    if not files:
        return

    semaphore = asyncio.Semaphore(args.concurrency)
    total = len(files)
    progress = {"done": 0, "uploaded": 0, "failed": 0}

    async def _worker(path: Path):
        async with semaphore:
            if args.delay:
                await asyncio.sleep(args.delay)

            try:
                result = await _upload_one(path, args.folder)
            except Exception as e:
                progress["done"] += 1
                progress["failed"] += 1
                print(f"<e> [{progress['done']}/{total}] upload failed for {path.stem}: {e}")
                return

            mapping.append({"pahe_id": path.stem, "cloudinary_url": result["secure_url"]})
            progress["done"] += 1
            progress["uploaded"] += 1
            print(f"  [{progress['done']}/{total}] uploaded: {path.stem}")

    await asyncio.gather(*(_worker(p) for p in files))

    print(
        f"Uploaded: {progress['uploaded']}  "
        f"Backfilled from Cloudinary: {backfilled}  "
        f"Already in mapping: {already_in_mapping}  "
        f"Failed: {progress['failed']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source_dir", help="Directory of local image files to upload")
    parser.add_argument(
        "--mapping-csv", required=True,
        help="Where to record {pahe_id, cloudinary_url} as uploads complete",
    )
    parser.add_argument("--folder", default=None, help="Optional Cloudinary folder to upload into")
    parser.add_argument("--concurrency", type=int, default=4, help="Max uploads in flight at once")
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="Fixed seconds to wait before each upload starts (per concurrent slot) - only needed if you hit rate limits",
    )
    args = parser.parse_args()

    _check_credentials()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
