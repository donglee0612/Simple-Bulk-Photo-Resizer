#!/usr/bin/env python3
"""
Fast recursive photo resizer.

Behavior:
- Searches the root folder and all subfolders.
- Processes JPG, JPEG, PNG, WEBP, HEIC, and HEIF images.
- Final image is always JPEG and <= 1,000,000 bytes.
- Keeps the ORIGINAL BASE FILENAME:
      IMG_1234.HEIC -> IMG_1234.jpeg
      beach.png     -> beach.jpeg
      photo.jpg     -> photo.jpeg
- Does NOT add the parent folder name.
- Deletes/replaces the original only AFTER the new file is successfully created.
- Uses multiple workers for faster bulk processing.
- JPG/JPEG images already <= 1 MB are not recompressed unnecessarily.
"""

import argparse
import io
import math
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:
    print("ERROR: Pillow is not installed.")
    print("Run: pip install pillow")
    sys.exit(1)

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_ENABLED = True
except ImportError:
    HEIF_ENABLED = False


MAX_BYTES = 1_000_000
START_QUALITY = 85
MIN_QUALITY = 45
MIN_LONG_EDGE = 800
DEFAULT_WORKERS = min(4, os.cpu_count() or 1)

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"
}


def destination_for(source: Path) -> Path:
    """Keep the original stem and use .jpeg as the final extension."""
    return source.with_name(f"{source.stem}.jpeg")


def same_path(a: Path, b: Path) -> bool:
    return (
        os.path.normcase(os.path.abspath(a))
        == os.path.normcase(os.path.abspath(b))
    )


def prepare_image(img: Image.Image) -> Image.Image:
    """Fix EXIF orientation and convert to JPEG-compatible RGB."""
    img = ImageOps.exif_transpose(img)

    if img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background

    if img.mode != "RGB":
        img = img.convert("RGB")

    return img


def encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()

    img.save(
        buffer,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=True,
    )

    return buffer.getvalue()


def resize_fast_to_limit(img: Image.Image):
    working = img
    quality = START_QUALITY

    for _ in range(12):
        data = encode_jpeg(working, quality)

        if len(data) <= MAX_BYTES:
            return data, quality, working.size

        width, height = working.size
        long_edge = max(width, height)

        if long_edge <= MIN_LONG_EDGE and quality > MIN_QUALITY:
            quality = max(MIN_QUALITY, quality - 10)
            continue

        ratio = math.sqrt(MAX_BYTES / len(data)) * 0.94
        ratio = min(ratio, 0.90)
        ratio = max(ratio, 0.50)

        new_width = max(1, int(width * ratio))
        new_height = max(1, int(height * ratio))

        working = working.resize(
            (new_width, new_height),
            Image.Resampling.LANCZOS,
        )

    while True:
        data = encode_jpeg(working, MIN_QUALITY)

        if len(data) <= MAX_BYTES:
            return data, MIN_QUALITY, working.size

        width, height = working.size

        working = working.resize(
            (
                max(1, int(width * 0.85)),
                max(1, int(height * 0.85)),
            ),
            Image.Resampling.LANCZOS,
        )


def atomic_write(destination: Path, data: bytes):
    """Write safely to a temporary file before replacing the final file."""
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.stem}_",
            suffix=".tmp",
            delete=False,
        ) as temp_file:

            temp_path = Path(temp_file.name)
            temp_file.write(data)

        os.replace(temp_path, destination)

    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

        raise


def rename_small_jpeg(source: Path, destination: Path):
    """Rename small JPG files to .jpeg without recompressing them."""

    if same_path(source, destination):
        return (
            "skipped",
            {
                "destination": source,
                "bytes": source.stat().st_size,
                "reason": "already <= 1 MB",
            },
        )

    if destination.exists():
        return (
            "error",
            f"Destination already exists: {destination}",
        )

    os.replace(source, destination)

    return (
        "renamed",
        {
            "destination": destination,
            "bytes": destination.stat().st_size,
            "reason": "already <= 1 MB; renamed without recompression",
        },
    )


def process_image(source: Path):

    suffix = source.suffix.lower()
    destination = destination_for(source)

    if suffix in {".heic", ".heif"} and not HEIF_ENABLED:
        return (
            "error",
            "HEIC/HEIF support is missing. Run: pip install pillow-heif",
        )

    try:

        # Fast path for JPG/JPEG already under 1 MB.
        if (
            suffix in {".jpg", ".jpeg"}
            and source.stat().st_size <= MAX_BYTES
        ):
            return rename_small_jpeg(source, destination)

        # Don't overwrite an unrelated file.
        if destination.exists() and not same_path(source, destination):
            return (
                "error",
                f"Destination already exists: {destination}",
            )

        with Image.open(source) as img:
            img.load()
            prepared = prepare_image(img)

        jpeg_data, quality, final_size = resize_fast_to_limit(prepared)

        # If source is already .jpeg, replace it safely.
        if same_path(source, destination):

            atomic_write(destination, jpeg_data)

        else:

            # Create the final JPEG first.
            atomic_write(destination, jpeg_data)

            # Only delete original AFTER success.
            try:
                source.unlink()

            except Exception as exc:
                return (
                    "warning",
                    {
                        "destination": destination,
                        "bytes": len(jpeg_data),
                        "quality": quality,
                        "dimensions": final_size,
                        "warning": (
                            "JPEG was created successfully, but the "
                            f"original could not be deleted: {exc}"
                        ),
                    },
                )

        return (
            "ok",
            {
                "destination": destination,
                "bytes": len(jpeg_data),
                "quality": quality,
                "dimensions": final_size,
            },
        )

    except Exception as exc:
        return "error", str(exc)


def find_images(root: Path):

    for path in root.rglob("*"):

        if (
            path.is_file()
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ):
            yield path


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Recursively resize photos to <=1 MB while keeping "
            "their original base filenames."
        )
    )

    parser.add_argument(
        "root",
        nargs="?",
        default=None,
        help=(
            "Root folder. Defaults to the folder containing "
            "this script."
        ),
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel workers. Default: {DEFAULT_WORKERS}",
    )

    args = parser.parse_args()

    if args.workers < 1:
        print("ERROR: --workers must be at least 1.")
        sys.exit(1)

    if args.root:
        root = Path(args.root).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parent

    if not root.exists() or not root.is_dir():
        print(
            f"ERROR: Root folder does not exist or "
            f"is not a folder:\n{root}"
        )
        sys.exit(1)

    images = list(find_images(root))

    print(f"Root folder: {root}")
    print(f"Maximum output size: {MAX_BYTES:,} bytes")
    print(f"Parallel workers: {args.workers}")
    print()

    if not images:
        print("No supported images found.")
        return

    print(f"Found {len(images)} image(s).\n")

    success = 0
    renamed = 0
    skipped = 0
    warnings = 0
    errors = 0

    with ThreadPoolExecutor(
        max_workers=args.workers
    ) as executor:

        futures = {
            executor.submit(process_image, source): source
            for source in images
        }

        completed = 0

        for future in as_completed(futures):

            source = futures[future]
            completed += 1

            try:
                status, result = future.result()

            except Exception as exc:
                status, result = "error", str(exc)

            rel_source = source.relative_to(root)

            print(
                f"[{completed}/{len(images)}] "
                f"{rel_source}"
            )

            if status == "ok":

                success += 1

                destination = result["destination"]
                rel_destination = destination.relative_to(root)

                size_kb = result["bytes"] / 1000
                width, height = result["dimensions"]

                print(
                    f"    -> {rel_destination}"
                    f" | {size_kb:.1f} KB"
                    f" | quality={result['quality']}"
                    f" | {width}x{height}"
                )

            elif status == "renamed":

                renamed += 1

                destination = result["destination"]
                rel_destination = destination.relative_to(root)

                print(
                    f"    -> {rel_destination}"
                    f" | {result['reason']}"
                )

            elif status == "skipped":

                skipped += 1
                print(
                    f"    SKIPPED: "
                    f"{result['reason']}"
                )

            elif status == "warning":

                warnings += 1
                print(
                    f"    WARNING: "
                    f"{result['warning']}"
                )

            else:

                errors += 1
                print(f"    ERROR: {result}")

    print("\nDone.")
    print(f"Created/resized: {success}")
    print(f"Renamed only:    {renamed}")
    print(f"Skipped:         {skipped}")
    print(f"Warnings:        {warnings}")
    print(f"Errors:          {errors}")

    if not HEIF_ENABLED:

        print(
            "\nHEIC/HEIF support is not installed.\n"
            "For iPhone photos, run:\n"
            "    pip install pillow-heif"
        )


if __name__ == "__main__":
    main()