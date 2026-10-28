"""Reproducible code-only local release; never copies keys, accounts or photos."""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def release_files(root):
    root=Path(root)
    files=[p for p in root.glob('*.py') if not p.name.startswith(('_','test'))]
    files += [p for p in root.glob('*.json') if p.name=='skill_template.lock.json']
    files += [p for p in root.glob('requirements*.txt')]
    for folder in ('web','vendor/ecom-details-image'):
        files += [p for p in (root/folder).rglob('*') if p.is_file() and not p.is_symlink() and not any(part in {'.git','__pycache__'} for part in p.relative_to(root).parts) and p.suffix in {'.py','.html','.js','.css','.json','.md','.png','.jpg','.webp'}]
    return sorted(set(files))


def manifest(root):
    root=Path(root)
    files={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in release_files(root)}
    digest=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
    return {'schema_version':1,'release_id':digest[:16],'python':sys.version.split()[0],'files':files}


def verify(root, release):
    root=Path(root); mismatches=[]
    for relative,digest in release['files'].items():
        path=(root/relative).resolve()
        if root.resolve() not in path.parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            mismatches.append(relative)
    return mismatches


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--destination');parser.add_argument('--verify',action='store_true'); args=parser.parse_args()
    release=manifest(ROOT)
    if args.verify:
        destination=Path(args.destination or ROOT)
        mismatches=verify(destination,release)
        print(json.dumps({'release_id':release['release_id'],'mismatches':mismatches},ensure_ascii=True));sys.exit(bool(mismatches))
    if args.destination:
        destination=Path(args.destination).resolve()
        if destination==ROOT or ROOT in destination.parents: raise SystemExit('Choose a separate runtime directory')
        # Code rollback contains exactly the files about to be replaced.
        rollback=destination/'.release_previous'
        for relative in release['files']:
            old=destination/relative
            if old.is_file():
                saved=rollback/relative;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(old,saved)
            target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/relative,target)
        (destination/'release_manifest.json').write_text(json.dumps(release,indent=2),'utf-8')
    (ROOT/'release_manifest.json').write_text(json.dumps(release,indent=2),'utf-8')
    print(json.dumps({'release_id':release['release_id'],'files':len(release['files'])},ensure_ascii=True))
