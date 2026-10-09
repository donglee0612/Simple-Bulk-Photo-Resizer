# Bulk Photo Resizer

`bulk_resize_photos.py` processes photos in a folder and all its subfolders, converting them to JPEG files no larger than **1 MB (1,000,000 bytes)**.

It supports JPG, JPEG, PNG, WEBP, HEIC, and HEIF. Photos stay in their original folders and keep their base names (for example, `IMG_1234.HEIC` becomes `IMG_1234.jpeg`). JPG/JPEG files already within the limit are not recompressed.

**Back up your photos first:** the script replaces or deletes originals after creating the output. Converted photos may lose metadata such as GPS information, and transparent backgrounds become white.

## Setup

Install Python, then run:

```sh
python -m pip install pillow pillow-heif
```

`pillow-heif` is only needed for HEIC/HEIF photos.

## Run

Open a terminal in the folder containing the script. To process that folder and its subfolders:

```sh
python bulk_resize_photos.py
```

To process a different folder:

```sh
python bulk_resize_photos.py "C:\path\to\photos"
```

The script displays progress and a summary when finished.
