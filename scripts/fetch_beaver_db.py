"""Download BEAVER's anonymized MySQL dump (beaver_db.zip, ~262 MB, gated on HF)
and print the commands to load it into the local reference MySQL server.

Prerequisite: accept the terms on https://huggingface.co/datasets/beaverbench/beaver-table
and run `hf auth login` once.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "data" / "beaver_db"


def main() -> None:
    from huggingface_hub import hf_hub_download

    zpath = Path(hf_hub_download("beaverbench/beaver-table", "beaver_db.zip", repo_type="dataset"))
    print(f"downloaded {zpath} ({zpath.stat().st_size / 1e6:.1f} MB)")
    DEST.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(DEST)
    dumps = sorted(DEST.rglob("*.sql"))
    if not dumps:
        sys.exit(f"no .sql files found under {DEST}; inspect the archive layout")
    print("\nLoad each dump into MySQL (you will be prompted for the password):")
    for d in dumps:
        print(f'  mysql -u root -p --default-character-set=utf8mb4 < "{d}"')


if __name__ == "__main__":
    main()
