"""Bridge from clean_vault.py into your Image Cropper / inventory tool.

Run with:  python clean_vault.py --apply --cropper cropper_hook:process

clean_vault calls process() once per NEW unique image (duplicates never get here),
with the raw bytes still in RAM. Replace the body with a call into your cropper.
"""

import logging

log = logging.getLogger("clean_vault.cropper")


def process(data: bytes, filename: str, category: str) -> None:
    # Example wiring, once your cropper exposes a function that takes bytes:
    #   from my_cropper import crop_and_log
    #   crop_and_log(io.BytesIO(data), sku_hint=filename, category=category)
    log.info("cropper hook: %s (%s, %d KB)", filename, category, len(data) // 1024)
