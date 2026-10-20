"""Local private recovery snapshot. Stop web server AND scheduler before running.
Includes an SQLite backup and uploaded originals, but never environment credentials.
Backups can contain personal/health information: keep them offline and private.
"""
import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import zipfile

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--database',default='instance/sahayak.sqlite3')
parser.add_argument('--output',default='backups')
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
source=Path(args.database).resolve()
if not source.is_file():
    parser.error('Database not found. Run this command from the project folder after starting the app once.')
out=Path(args.output).resolve()/datetime.now(timezone.utc).strftime('sahayak-%Y%m%d-%H%M%S-%f')
out.mkdir(parents=True,exist_ok=False)
with sqlite3.connect(f'{source.as_uri()}?mode=ro',uri=True) as db, sqlite3.connect(out/'sahayak.sqlite3') as backup:
    db.backup(backup)
# Upload directory is fixed by this application; backup snapshots must be consistent.
uploads=root/'instance'/'uploads'
if uploads.exists():
    shutil.copytree(uploads,out/'uploads')
excluded={'.venv','venv','env','__pycache__','.git','.pytest_cache','instance','backups','browser-artifacts'}
with zipfile.ZipFile(out/'source.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for directory, dirs, files in os.walk(root):
        dirs[:]=[d for d in dirs if d not in excluded and Path(directory)/d!=out]
        for name in files:
            path=Path(directory)/name
            if path.is_symlink() or (name.startswith('.env') and name!='.env.example'):
                continue
            if path.suffix in ('.pyc','.db','.sqlite3','.log'):
                continue
            archive.write(path,Path('sahayak')/path.relative_to(root))
(out/'PRIVATE_BACKUP_WARNING.txt').write_text('PRIVATE local recovery backup. Contains database and uploaded documents. Never commit, email or upload it publicly. Credentials and session secret were NOT copied. Stop both processes before restoring. See README recovery instructions.\n',encoding='utf-8')
print('Private backup created at:',out)
print('Keep this folder offline. It is NOT safe for public sharing.')
