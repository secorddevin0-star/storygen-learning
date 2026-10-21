# clean_vault — Phone → Drive → Canva sorter

Your phone dumps images and ZIPs into `RAW_DROP` on Google Drive. This script removes duplicates, sorts what's left by product into `PRODUCTION_VAULT/<Category>/Batch_N` folders of 250 files or fewer, and can pass each new image to your cropper.

Nothing is saved to your laptop's disk. ZIPs are read in RAM. Loose files are never downloaded, because Drive already stores a SHA-256 fingerprint for every file.

## What it does

| In `RAW_DROP` | Result |
|---|---|
| New image | **Moved** (not copied) into `PRODUCTION_VAULT/<Category>/Batch_N` |
| Image already in the vault, or already seen in this run | Sent to **Drive Trash** (you can restore it for 30 days) |
| ZIP | Read in RAM. Each new image is uploaded to the vault. Duplicates are skipped. |
| ZIP that contained only images | Sent to Trash after every image is safely in the vault |
| ZIP that also contained PSDs, fonts or licence files | Moved to `PRODUCTION_VAULT/_ZIP_ARCHIVE` so you keep those files |
| ZIP where any upload failed | Left in `RAW_DROP`. Run the script again to retry. |
| Non-image files (`.txt`, `.pdf`, …) | Left where they are |
| `__MACOSX`, `.DS_Store`, `._*` | Ignored |

Categories are checked in this order, and the first match wins: Hoodies (`hoodie`, `sweatshirt`) → T-Shirts (`tshirt`, `t-shirt`, `tee`) → Hats (`hat`, `cap`, `beanie`, `snapback`) → Mugs (`mug`, `cup`) → Bags (`tote`, `bag`). Anything else goes to `Uncategorized_Mockups`. The script checks the folder path and the file name, and inside a ZIP it also checks the ZIP's name. Short keywords must be a whole word, so `that.png` will not land in Hats and `landscape.png` will not land in Hats as a cap.

Each run rebuilds its duplicate list from what is already in `PRODUCTION_VAULT`. There's no local database to lose. Running it twice is safe.

## Setup (once, on Windows)

1. **Install Python 3.10 or newer** from python.org. During install, tick "Add to PATH".
2. **Install the libraries.** In PowerShell, from this folder:
   ```powershell
   pip install -r requirements.txt
   ```
3. **Create the Google login file.**
   - Go to console.cloud.google.com. Create a project and enable the **Google Drive API**.
   - Open **OAuth consent screen**. Choose External, fill in the required fields, and add your own Gmail under **Test users**.
   - Open **Credentials**. Choose Create credentials → OAuth client ID → **Desktop app**. Download the JSON file.
   - Save it in this folder as `credentials.json`.
4. **Create a folder named `RAW_DROP`** in Google Drive from your phone, and upload into it.

> **Gotcha:** while your OAuth app is in "Testing" mode, Google expires the login after 7 days. When that happens, the script opens the browser so you can log in again. That's normal. To stop it, publish the app on the consent screen. A personal app does not need Google's review for that.

`credentials.json` and `token.json` are your keys. They are already git-ignored. Never share them.

## Run it

```powershell
python clean_vault.py            # DRY RUN: shows what it would do, changes nothing
python clean_vault.py --apply    # do it
```

Always run the dry run first, and read the summary at the end. The first run opens your browser once so you can log in.

Useful options:

| Option | Default | When to use it |
|---|---|---|
| `--batch-size 250` | 250 | Change it if Canva imports still time out |
| `--workers 8` | 8 | Lower it if Google shows rate-limit errors |
| `--max-zip-mb 2048` | 2 GB | ZIPs larger than this are skipped, to protect your RAM |
| `--raw-drop-id <id>` | – | Use this if you have more than one folder named `RAW_DROP` |
| `--cropper cropper_hook:process` | off | Send each new unique image to your cropper |

## Connecting your cropper

Open `cropper_hook.py` and replace the body of `process(data, filename, category)` with a call into your cropper. `data` holds the image's bytes, in RAM. Only new, unique images are sent. If the cropper crashes on an image, the script logs the error and keeps sorting.

## Canva

In Canva, open Uploads → Import → Google Drive. Pick one `Batch_N` folder at a time.

The 250-files-per-folder cap comes from the original plan. It has not been checked against Canva's documentation. If imports work well with larger batches, raise `--batch-size`. Folders that already exist keep the files they have.

## Tests

```powershell
pip install pytest
python -m pytest test_clean_vault.py
```

The tests run the whole pipeline against a fake in-memory Drive, so they don't touch your account.
