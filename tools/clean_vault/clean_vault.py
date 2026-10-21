"""clean_vault.py - Phone-to-cloud deduplicator and Canva sorter for Google Drive.

Pipeline:
  RAW_DROP (phone dumps)  ->  SHA-256 dedup  ->  category sort  ->
  PRODUCTION_VAULT/<Category>/Batch_N (max 250 per batch)  ->  optional cropper hook

Nothing is written to local disk. ZIPs are downloaded into RAM (io.BytesIO) and
read in place. Loose files never need downloading at all: Drive already stores a
SHA-256 for every uploaded file, so they are deduped and moved server-side.

Safe by default: without --apply the script only reports what it WOULD do.

Usage:
  python clean_vault.py            # dry run, prints the plan
  python clean_vault.py --apply    # do it
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import logging
import mimetypes
import os
import re
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".svg", ".tif", ".tiff", ".bmp"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
ZIP_ARCHIVE_FOLDER = "_ZIP_ARCHIVE"
UNCATEGORIZED = "Uncategorized_Mockups"

# Checked in order. Hoodies goes first because "sweatshirt" contains "tshirt".
# "long" keywords match inside a word (catches "whitetshirtmockup").
# "short" keywords must be a whole word, so "hat" won't match "that"/"chat",
# "cap" won't match "landscape", "cup" won't match "cupcake".
CATEGORY_RULES = [
    ("Hoodies", {"long": ["hoodie", "sweatshirt"], "short": ["hoody"]}),
    ("T-Shirts", {"long": ["tshirt"], "short": ["tee"]}),
    ("Hats", {"long": ["snapback", "beanie"], "short": ["hat", "cap"]}),
    ("Mugs", {"long": [], "short": ["mug", "cup"]}),
    ("Bags", {"long": ["totebag"], "short": ["tote", "bag"]}),
]

mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("image/webp", ".webp")

log = logging.getLogger("clean_vault")


# --------------------------------------------------------------------------
# Pure helpers (no Drive access - unit tested)
# --------------------------------------------------------------------------

def tokenize(path: str) -> list[str]:
    """Split a path/filename into lowercase words, also splitting camelCase."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", path)
    text = text.lower()
    text = re.sub(r"(?<![a-z])t[\s_\-.]?shirt", "tshirt", text)
    # Letters and digits are separate words: "mug3" -> "mug", "3".
    return re.findall(r"[a-z]+|[0-9]+", text)


def categorize(path: str) -> str:
    tokens = tokenize(path)
    for category, kw in CATEGORY_RULES:
        if any(k in t for t in tokens for k in kw["long"]):
            return category
        for k in kw["short"]:
            if any(t in (k, k + "s", k + "es") for t in tokens):
                return category
    return UNCATEGORIZED


def is_junk(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    if "__MACOSX" in parts:
        return True
    name = parts[-1]
    return name.lower() in JUNK_NAMES or name.startswith("._")


def is_image(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class HashIndex:
    """Thread-safe set of seen SHA-256 digests."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def add(self, digest: str) -> None:
        with self._lock:
            self._seen.add(digest)

    def claim(self, digest: str) -> bool:
        """Return True if digest is new (and reserve it), False if duplicate."""
        with self._lock:
            if digest in self._seen:
                return False
            self._seen.add(digest)
            return True

    def release(self, digest: str) -> None:
        """Undo a claim when the file it was claimed for failed to land."""
        with self._lock:
            self._seen.discard(digest)

    def __len__(self) -> int:
        return len(self._seen)


@dataclass
class Batch:
    number: int
    folder_id: str
    count: int


class BatchAllocator:
    """Hands out the folder each new file goes to, creating Batch_N as needed.

    create_folder(name, parent_id) -> folder_id is injected so this can be
    tested without Drive and so dry runs never create anything.
    """

    def __init__(self, vault_id: str, batch_size: int, create_folder: Callable[[str, str], str]):
        self.vault_id = vault_id
        self.batch_size = batch_size
        self._create_folder = create_folder
        self._lock = threading.Lock()
        self._category_ids: dict[str, str] = {}
        self._batches: dict[str, list[Batch]] = {}

    def seed_category(self, category: str, folder_id: str) -> None:
        self._category_ids[category] = folder_id

    def seed_batch(self, category: str, number: int, folder_id: str, count: int) -> None:
        self._batches.setdefault(category, []).append(Batch(number, folder_id, count))

    def allocate(self, category: str) -> str:
        with self._lock:
            cat_id = self._category_ids.get(category)
            if cat_id is None:
                cat_id = self._create_folder(category, self.vault_id)
                self._category_ids[category] = cat_id
            batches = self._batches.setdefault(category, [])
            open_batches = sorted((b for b in batches if b.count < self.batch_size), key=lambda b: b.number)
            if open_batches:
                batch = open_batches[0]
            else:
                number = max((b.number for b in batches), default=0) + 1
                batch = Batch(number, self._create_folder(f"Batch_{number}", cat_id), 0)
                batches.append(batch)
            batch.count += 1
            return batch.folder_id

    def summary(self) -> dict[str, list[tuple[int, int]]]:
        return {c: sorted((b.number, b.count) for b in bs) for c, bs in self._batches.items()}


@dataclass
class Stats:
    moved: int = 0
    uploaded_from_zip: int = 0
    duplicates_trashed: int = 0
    zip_dupes_skipped: int = 0
    zips_trashed: int = 0
    zips_archived: int = 0
    skipped: int = 0
    errors: int = 0
    cropper_calls: int = 0
    per_category: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def bump(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def landed(self, category: str) -> None:
        with self._lock:
            self.per_category[category] = self.per_category.get(category, 0) + 1


# --------------------------------------------------------------------------
# Google Drive access
# --------------------------------------------------------------------------

def get_credentials(credentials_path: str, token_path: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # refresh token revoked/expired -> log in again
            log.warning("Saved login expired (%s). Opening browser to log in again.", exc)
            creds = None
    if not creds or not creds.valid:
        if not os.path.exists(credentials_path):
            sys.exit(f"Missing {credentials_path}. See README step 2 (Google Cloud OAuth client).")
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
        creds = flow.run_local_server(port=0)
    with open(token_path, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    return creds


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    """Thin Drive v3 wrapper. Each thread gets its own client (httplib2 is not thread-safe)."""

    RETRIES = 5
    FILE_FIELDS = "id,name,mimeType,size,sha256Checksum,parents"

    def __init__(self, creds):
        self._creds = creds
        self._local = threading.local()

    @property
    def svc(self):
        if not hasattr(self._local, "svc"):
            from googleapiclient.discovery import build
            self._local.svc = build("drive", "v3", credentials=self._creds, cache_discovery=False)
        return self._local.svc

    def list_children(self, folder_id: str) -> list[dict]:
        items, token = [], None
        while True:
            resp = self.svc.files().list(
                q=f"'{_q(folder_id)}' in parents and trashed = false",
                fields=f"nextPageToken, files({self.FILE_FIELDS})",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(num_retries=self.RETRIES)
            items.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return items

    def find_folders(self, name: str, parent_id: Optional[str] = None) -> list[dict]:
        q = f"name = '{_q(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
        if parent_id:
            q += f" and '{_q(parent_id)}' in parents"
        resp = self.svc.files().list(
            q=q, fields="files(id,name)", pageSize=10,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute(num_retries=self.RETRIES)
        return resp.get("files", [])

    def create_folder(self, name: str, parent_id: str) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        resp = self.svc.files().create(body=body, fields="id", supportsAllDrives=True).execute(num_retries=self.RETRIES)
        log.info("Created folder %s", name)
        return resp["id"]

    def download(self, file_id: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        buf = io.BytesIO()
        request = self.svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        downloader = MediaIoBaseDownload(buf, request, chunksize=32 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=self.RETRIES)
        return buf.getvalue()

    def upload(self, data: bytes, name: str, parent_id: str) -> str:
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=guess_mime(name), resumable=len(data) > 5 * 1024 * 1024)
        resp = self.svc.files().create(
            body={"name": name, "parents": [parent_id]}, media_body=media,
            fields="id", supportsAllDrives=True,
        ).execute(num_retries=self.RETRIES)
        return resp["id"]

    def move(self, file_id: str, new_parent: str, old_parents: list[str]) -> None:
        self.svc.files().update(
            fileId=file_id, addParents=new_parent, removeParents=",".join(old_parents),
            fields="id", supportsAllDrives=True,
        ).execute(num_retries=self.RETRIES)

    def trash(self, file_id: str) -> None:
        self.svc.files().update(
            fileId=file_id, body={"trashed": True}, fields="id", supportsAllDrives=True,
        ).execute(num_retries=self.RETRIES)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

CropperFn = Callable[[bytes, str, str], None]


def load_cropper(spec: Optional[str]) -> Optional[CropperFn]:
    """--cropper my_cropper:process  ->  my_cropper.process(data, filename, category)."""
    if not spec:
        return None
    module_name, _, func_name = spec.partition(":")
    sys.path.insert(0, SCRIPT_DIR)
    return getattr(importlib.import_module(module_name), func_name or "process")


class Pipeline:
    def __init__(self, drive: Drive, args, cropper: Optional[CropperFn]):
        self.drive = drive
        self.args = args
        self.apply = args.apply
        self.cropper = cropper
        self.index = HashIndex()
        self.stats = Stats()
        self.vault_id = ""
        self.archive_id: Optional[str] = None
        self.allocator: Optional[BatchAllocator] = None
        self._archive_lock = threading.Lock()
        self._upload_pool = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="upload")

    # ---- setup -----------------------------------------------------------

    def _create_folder(self, name: str, parent_id: str) -> str:
        if not self.apply:
            log.info("[dry run] would create folder %s", name)
            return f"dry-run:{parent_id}/{name}"
        return self.drive.create_folder(name, parent_id)

    def locate_raw_drop(self) -> str:
        if self.args.raw_drop_id:
            return self.args.raw_drop_id
        found = self.drive.find_folders(self.args.raw_drop_name)
        if not found:
            sys.exit(f"No folder named '{self.args.raw_drop_name}' found in Google Drive.")
        if len(found) > 1:
            ids = ", ".join(f["id"] for f in found)
            sys.exit(f"Several folders named '{self.args.raw_drop_name}' ({ids}). Pass --raw-drop-id.")
        return found[0]["id"]

    def prepare_vault(self) -> None:
        found = self.drive.find_folders(self.args.vault_name, parent_id="root")
        self.vault_id = found[0]["id"] if found else self._create_folder(self.args.vault_name, "root")
        self.allocator = BatchAllocator(self.vault_id, self.args.batch_size, self._create_folder)
        if not found:
            return

        # Rebuild state from what's already in the vault: no local database to lose.
        batch_re = re.compile(r"^Batch_(\d+)$")
        for cat in self.drive.list_children(self.vault_id):
            if cat["mimeType"] != FOLDER_MIME:
                self._index_file(cat)
                continue
            if cat["name"] == ZIP_ARCHIVE_FOLDER:
                self.archive_id = cat["id"]
            else:
                self.allocator.seed_category(cat["name"], cat["id"])
            self._index_tree(cat["id"], cat["name"], batch_re)
        log.info("Vault indexed: %d known files", len(self.index))

    def _index_tree(self, folder_id: str, category: str, batch_re) -> None:
        for item in self.drive.list_children(folder_id):
            if item["mimeType"] != FOLDER_MIME:
                self._index_file(item)
                continue
            children = self.drive.list_children(item["id"])
            m = batch_re.match(item["name"])
            if m and category != ZIP_ARCHIVE_FOLDER:
                count = sum(1 for c in children if c["mimeType"] != FOLDER_MIME)
                self.allocator.seed_batch(category, int(m.group(1)), item["id"], count)
            for child in children:
                if child["mimeType"] == FOLDER_MIME:
                    self._index_tree(child["id"], category, batch_re)
                else:
                    self._index_file(child)

    def _index_file(self, item: dict) -> None:
        if item.get("sha256Checksum"):
            self.index.add(item["sha256Checksum"])

    def walk_raw_drop(self, folder_id: str, prefix: str = "") -> list[tuple[dict, str]]:
        out = []
        for item in self.drive.list_children(folder_id):
            rel = f"{prefix}{item['name']}"
            if item["mimeType"] == FOLDER_MIME:
                out.extend(self.walk_raw_drop(item["id"], rel + "/"))
            else:
                out.append((item, rel))
        return out

    # ---- per-file work ---------------------------------------------------

    def run(self) -> Stats:
        raw_id = self.locate_raw_drop()
        self.prepare_vault()
        files = self.walk_raw_drop(raw_id)
        log.info("RAW_DROP: %d files to check", len(files))

        # Loose images first so a loose copy wins over the same image inside a ZIP.
        loose = [fr for fr in files if not fr[1].lower().endswith(".zip")]
        zips = [fr for fr in files if fr[1].lower().endswith(".zip")]

        for group in (loose, zips):
            with ThreadPoolExecutor(max_workers=self.args.workers, thread_name_prefix="file") as pool:
                futures = {pool.submit(self._handle, item, rel): rel for item, rel in group}
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        self.stats.bump("errors")
                        log.exception("Failed: %s", futures[fut])
        self._upload_pool.shutdown(wait=True)
        return self.stats

    def _handle(self, item: dict, rel: str) -> None:
        if is_junk(rel):
            self.stats.bump("skipped")
            return
        if rel.lower().endswith(".zip"):
            self._handle_zip(item, rel)
        elif is_image(rel) or item.get("mimeType", "").startswith("image/"):
            self._handle_loose(item, rel)
        else:
            log.info("Skip (not an image): %s", rel)
            self.stats.bump("skipped")

    def _handle_loose(self, item: dict, rel: str) -> None:
        data: Optional[bytes] = None
        digest = item.get("sha256Checksum")
        if not digest:  # rare: Drive hasn't computed it - hash in RAM
            data = self.drive.download(item["id"])
            digest = sha256_bytes(data)

        if not self.index.claim(digest):
            self._trash(item["id"], f"duplicate: {rel}")
            self.stats.bump("duplicates_trashed")
            return

        category = categorize(rel)
        try:
            target = self.allocator.allocate(category)
            if self.apply:
                self.drive.move(item["id"], target, item.get("parents", []))
            log.info("%s %s -> %s", "Moved" if self.apply else "[dry run] would move", rel, category)
        except Exception:
            self.index.release(digest)
            raise
        self.stats.bump("moved")
        self.stats.landed(category)

        if self.cropper and self.apply:
            if data is None:
                data = self.drive.download(item["id"])
            self._crop(data, item["name"], category)

    def _handle_zip(self, item: dict, rel: str) -> None:
        digest = item.get("sha256Checksum")
        if digest and not self.index.claim(digest):
            self._trash(item["id"], f"duplicate zip: {rel}")
            self.stats.bump("duplicates_trashed")
            return

        size_mb = int(item.get("size", 0)) / (1024 * 1024)
        if size_mb > self.args.max_zip_mb:
            log.warning("Skip %s: %.0f MB is over --max-zip-mb %d", rel, size_mb, self.args.max_zip_mb)
            self.stats.bump("skipped")
            return

        log.info("Reading %s (%.1f MB) in RAM", rel, size_mb)
        raw = self.drive.download(item["id"])
        zip_stem = rel[:-4]
        leftovers = 0  # entries we did NOT put in the vault (PSDs, fonts, licences...)
        uploads = []

        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for info in zf.infolist():
                if info.is_dir() or is_junk(info.filename):
                    continue
                if not is_image(info.filename) or info.file_size > self.args.max_entry_mb * 1024 * 1024:
                    leftovers += 1
                    continue
                data = zf.read(info)
                entry_digest = sha256_bytes(data)
                if not self.index.claim(entry_digest):
                    self.stats.bump("zip_dupes_skipped")
                    continue
                name = os.path.basename(info.filename)
                category = categorize(f"{zip_stem}/{info.filename}")
                uploads.append(self._upload_pool.submit(self._upload_entry, data, name, category, entry_digest))
        del raw

        failed = 0
        for fut in uploads:
            try:
                fut.result()
            except Exception:
                failed += 1
                log.exception("Upload from %s failed", rel)
        if failed:
            self.stats.bump("errors", failed)
            log.warning("Keeping %s in RAW_DROP: %d entries failed. Re-run to retry.", rel, failed)
            return

        if leftovers:
            # Non-image files inside (e.g. PSD source files) - don't destroy them.
            self._archive_zip(item, rel, leftovers)
        else:
            self._trash(item["id"], f"zip fully ingested: {rel}")
            self.stats.bump("zips_trashed")

    def _upload_entry(self, data: bytes, name: str, category: str, digest: str) -> None:
        try:
            target = self.allocator.allocate(category)
            if self.apply:
                self.drive.upload(data, name, target)
            log.info("%s %s -> %s", "Uploaded" if self.apply else "[dry run] would upload", name, category)
        except Exception:
            self.index.release(digest)
            raise
        self.stats.bump("uploaded_from_zip")
        self.stats.landed(category)
        self._crop(data, name, category)

    def _archive_zip(self, item: dict, rel: str, leftovers: int) -> None:
        with self._archive_lock:
            if self.archive_id is None:
                self.archive_id = self._create_folder(ZIP_ARCHIVE_FOLDER, self.vault_id)
        if self.apply:
            self.drive.move(item["id"], self.archive_id, item.get("parents", []))
        log.info("%s %s to %s (%d non-image files kept inside)",
                 "Archived" if self.apply else "[dry run] would archive", rel, ZIP_ARCHIVE_FOLDER, leftovers)
        self.stats.bump("zips_archived")

    def _trash(self, file_id: str, why: str) -> None:
        if self.apply:
            self.drive.trash(file_id)
            log.info("Trashed (%s)", why)
        else:
            log.info("[dry run] would trash (%s)", why)

    def _crop(self, data: bytes, name: str, category: str) -> None:
        if not self.cropper or not self.apply:
            return
        try:
            self.cropper(data, name, category)
            self.stats.bump("cropper_calls")
        except Exception:
            # The cropper is downstream; a crop failure must not undo a clean sort.
            log.exception("Cropper failed on %s", name)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Dedupe RAW_DROP into PRODUCTION_VAULT for Canva.")
    p.add_argument("--apply", action="store_true", help="Actually move/upload/trash. Without it: dry run.")
    p.add_argument("--raw-drop-name", default="RAW_DROP")
    p.add_argument("--raw-drop-id", help="Use this folder ID if several folders share the name.")
    p.add_argument("--vault-name", default="PRODUCTION_VAULT")
    p.add_argument("--batch-size", type=int, default=250, help="Max files per Batch_N folder.")
    p.add_argument("--workers", type=int, default=8, help="Parallel Drive operations.")
    p.add_argument("--max-zip-mb", type=int, default=2048, help="Skip ZIPs bigger than this (RAM guard).")
    p.add_argument("--max-entry-mb", type=int, default=200, help="Skip single images inside ZIPs bigger than this.")
    p.add_argument("--cropper", help="Hook as module:function, e.g. cropper_hook:process")
    p.add_argument("--credentials", default=os.path.join(SCRIPT_DIR, "credentials.json"))
    p.add_argument("--token", default=os.path.join(SCRIPT_DIR, "token.json"))
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def print_summary(stats: Stats, allocator: Optional[BatchAllocator], apply: bool) -> None:
    mode = "DONE" if apply else "DRY RUN - nothing was changed. Re-run with --apply."
    print("\n" + "=" * 60)
    print(mode)
    print("=" * 60)
    print(f"Loose images moved to vault : {stats.moved}")
    print(f"Images uploaded from ZIPs   : {stats.uploaded_from_zip}")
    print(f"Duplicates sent to Trash    : {stats.duplicates_trashed}")
    print(f"Duplicate images inside ZIPs: {stats.zip_dupes_skipped}")
    print(f"ZIPs trashed (fully used)   : {stats.zips_trashed}")
    print(f"ZIPs archived (had extras)  : {stats.zips_archived}")
    print(f"Skipped (not images/junk)   : {stats.skipped}")
    print(f"Errors                      : {stats.errors}")
    if stats.cropper_calls:
        print(f"Sent to cropper             : {stats.cropper_calls}")
    if allocator:
        print("\nVault layout after this run:")
        for cat, batches in sorted(allocator.summary().items()):
            print(f"  {cat}: " + ", ".join(f"Batch_{n} ({c})" for n, c in batches))


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)

    creds = get_credentials(args.credentials, args.token)
    pipeline = Pipeline(Drive(creds), args, load_cropper(args.cropper))
    stats = pipeline.run()
    print_summary(stats, pipeline.allocator, args.apply)
    return 1 if stats.errors else 0


if __name__ == "__main__":
    sys.exit(main())
