"""Online-consistent SQLite backup; run locally or inside the API container."""
import argparse
import sqlite3
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('source');parser.add_argument('destination')
args=parser.parse_args()
target=Path(args.destination).resolve()
if target.exists():raise SystemExit('Destination exists; choose a new backup filename.')
target.parent.mkdir(parents=True,exist_ok=True)
with sqlite3.connect(f'file:{Path(args.source).resolve().as_posix()}?mode=ro',uri=True) as source,sqlite3.connect(target) as destination:
    source.backup(destination)
target.chmod(0o600)
print('Backup created:',target)
