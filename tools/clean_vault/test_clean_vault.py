import pytest

from clean_vault import BatchAllocator, HashIndex, categorize, is_image, is_junk


@pytest.mark.parametrize("path,expected", [
    ("White Tshirt Mockup.png", "T-Shirts"),
    ("WhiteTshirtMockup.png", "T-Shirts"),
    ("black-t-shirt_front.jpg", "T-Shirts"),
    ("bella canvas tee.png", "T-Shirts"),
    ("pack/Unisex Sweatshirt.png", "Hoodies"),
    ("hoodies/front.png", "Hoodies"),
    ("dad hat flat lay.jpg", "Hats"),
    ("Trucker Caps/1.png", "Hats"),
    ("11oz mug.png", "Mugs"),
    ("coffee cup.jpg", "Mugs"),
    ("canvas tote bag.png", "Bags"),
    # Lookalike words must NOT match short keywords.
    ("landscape capture.png", "Uncategorized_Mockups"),
    ("cupcake that chat.png", "Uncategorized_Mockups"),
    ("steel committee.png", "Uncategorized_Mockups"),
    ("short shirt.png", "Uncategorized_Mockups"),
    ("IMG_4021.HEIC", "Uncategorized_Mockups"),
    ("mug3.png", "Mugs"),
    ("hat01_black.jpg", "Hats"),
])
def test_categorize(path, expected):
    assert categorize(path) == expected


def test_junk_and_images():
    assert is_junk("__MACOSX/pack/._shirt.png")
    assert is_junk("pack/.DS_Store")
    assert is_junk("pack/._shirt.png")
    assert not is_junk("pack/shirt.png")
    assert is_image("a.HEIC") and is_image("b.jpeg")
    assert not is_image("source.psd") and not is_image("license.txt")


def test_hash_index_claim_release():
    idx = HashIndex()
    assert idx.claim("a")
    assert not idx.claim("a")
    idx.release("a")
    assert idx.claim("a")


def test_batches_cap_and_resume():
    created = []

    def create(name, parent):
        created.append((name, parent))
        return f"{parent}/{name}"

    alloc = BatchAllocator("vault", batch_size=3, create_folder=create)
    alloc.seed_category("Mugs", "vault/Mugs")
    alloc.seed_batch("Mugs", 1, "vault/Mugs/Batch_1", 2)  # existing, 1 slot left

    targets = [alloc.allocate("Mugs") for _ in range(5)]
    assert targets == ["vault/Mugs/Batch_1"] + ["vault/Mugs/Batch_2"] * 3 + ["vault/Mugs/Batch_3"]
    assert created == [("Batch_2", "vault/Mugs"), ("Batch_3", "vault/Mugs")]

    alloc.allocate("Hats")
    assert ("Hats", "vault") in created and ("Batch_1", "vault/Hats") in created
    assert alloc.summary()["Mugs"] == [(1, 3), (2, 3), (3, 1)]


# --------------------------------------------------------------------------
# End-to-end run against an in-memory fake Drive
# --------------------------------------------------------------------------

import hashlib
import io
import itertools
import zipfile

from clean_vault import FOLDER_MIME, Pipeline, parse_args


class FakeDrive:
    def __init__(self):
        self.items = {"root": {"id": "root", "name": "My Drive", "mimeType": FOLDER_MIME, "parents": []}}
        self.blobs = {}
        self._ids = itertools.count(1)

    def add(self, name, parent, data=None):
        fid = f"f{next(self._ids)}"
        item = {"id": fid, "name": name, "parents": [parent], "trashed": False,
                "mimeType": FOLDER_MIME if data is None else "application/octet-stream"}
        if data is not None:
            item["size"] = str(len(data))
            item["sha256Checksum"] = hashlib.sha256(data).hexdigest()
            self.blobs[fid] = data
        self.items[fid] = item
        return fid

    def list_children(self, folder_id):
        return [dict(i) for i in self.items.values() if folder_id in i["parents"] and not i.get("trashed")]

    def find_folders(self, name, parent_id=None):
        return [i for i in self.items.values() if i["name"] == name and i["mimeType"] == FOLDER_MIME
                and not i.get("trashed") and (parent_id is None or parent_id in i["parents"])]

    def create_folder(self, name, parent_id):
        return self.add(name, parent_id)

    def download(self, file_id):
        return self.blobs[file_id]

    def upload(self, data, name, parent_id):
        return self.add(name, parent_id, data)

    def move(self, file_id, new_parent, old_parents):
        self.items[file_id]["parents"] = [new_parent]

    def trash(self, file_id):
        self.items[file_id]["trashed"] = True

    def path_of(self, fid):
        parts = []
        while fid != "root":
            parts.append(self.items[fid]["name"])
            fid = self.items[fid]["parents"][0]
        return "/".join(reversed(parts))

    def live_files(self, under):
        return sorted(self.path_of(i["id"]) for i in self.items.values()
                      if i["mimeType"] != FOLDER_MIME and not i.get("trashed")
                      and self.path_of(i["id"]).startswith(under))


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def build_drive():
    d = FakeDrive()
    raw = d.add("RAW_DROP", "root")
    sub = d.add("hoodie pack", raw)
    d.add("tee_front.png", raw, b"TEE")
    d.add("tee_front copy.png", raw, b"TEE")  # duplicate loose
    d.add("front.png", sub, b"HOODIE")  # category from folder name
    d.add("notes.txt", raw, b"hello")
    d.add("mugs.zip", raw, make_zip({"mug1.png": b"MUG1", "mug2.png": b"MUG2", "dupe.png": b"TEE",
                                     "__MACOSX/._mug1.png": b"junk"}))
    d.add("mugs (1).zip", raw, make_zip({"mug1.png": b"MUG1", "mug2.png": b"MUG2", "dupe.png": b"TEE",
                                         "__MACOSX/._mug1.png": b"junk"}))  # byte-identical zip
    d.add("bags.zip", raw, make_zip({"tote.png": b"TOTE", "source.psd": b"PSD"}))
    return d


def run(d, *flags):
    args = parse_args(["--batch-size", "2", "--workers", "4", *flags])
    p = Pipeline(d, args, cropper=None)
    return p, p.run()


def test_dry_run_changes_nothing():
    d = build_drive()
    before = {k: dict(v) for k, v in d.items.items()}
    _, stats = run(d)
    assert d.items == before
    assert stats.moved == 2 and stats.uploaded_from_zip == 3 and stats.duplicates_trashed == 2


def test_apply_end_to_end_and_rerun_is_idempotent():
    d = build_drive()
    _, stats = run(d, "--apply")
    assert stats.errors == 0
    vault = d.live_files("PRODUCTION_VAULT")
    tees = [f for f in vault if f.startswith("PRODUCTION_VAULT/T-Shirts/Batch_1/tee_front")]
    assert len(tees) == 1  # which of the two identical copies wins is a thread race; one must
    assert [f for f in vault if f not in tees] == [
        "PRODUCTION_VAULT/Bags/Batch_1/tote.png",
        "PRODUCTION_VAULT/Hoodies/Batch_1/front.png",
        "PRODUCTION_VAULT/Mugs/Batch_1/mug1.png",
        "PRODUCTION_VAULT/Mugs/Batch_1/mug2.png",
        "PRODUCTION_VAULT/_ZIP_ARCHIVE/bags.zip",  # kept: has a PSD inside
    ]
    assert d.live_files("RAW_DROP") == ["RAW_DROP/notes.txt"]

    # Phone drops the same images again + one new mug: only the new one lands, batch rolls over.
    raw = d.find_folders("RAW_DROP")[0]["id"]
    d.add("again.png", raw, b"MUG1")
    d.add("mug3.png", raw, b"MUG3")
    _, stats2 = run(d, "--apply")
    assert stats2.duplicates_trashed == 1 and stats2.moved == 1
    assert "PRODUCTION_VAULT/Mugs/Batch_2/mug3.png" in d.live_files("PRODUCTION_VAULT")


def test_cropper_receives_unique_images_only():
    d = build_drive()
    seen = []
    args = parse_args(["--apply", "--workers", "4"])
    Pipeline(d, args, cropper=lambda data, name, cat: seen.append((data, cat))).run()
    assert sorted(seen) == sorted([(b"TEE", "T-Shirts"), (b"HOODIE", "Hoodies"), (b"MUG1", "Mugs"),
                                   (b"MUG2", "Mugs"), (b"TOTE", "Bags")])
