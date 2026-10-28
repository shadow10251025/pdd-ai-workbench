"""Operator-only backup and reference-aware cleanup; no public HTTP endpoint."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def _paths(value):
    if isinstance(value, dict):
        for item in value.values(): yield from _paths(item)
    elif isinstance(value, list):
        for item in value: yield from _paths(item)
    elif isinstance(value, str):
        yield value.replace('\\','/')


def live_references(root, keep_task_days=30):
    root=Path(root).resolve(); references=set(); pending=[]
    database=root/'data/workbench.sqlite3'
    if database.exists():
        with closing(sqlite3.connect(database)) as conn:
            for table,fields in (('drafts',('data',)),('tasks',('payload','result','checkpoint'))):
                query='SELECT '+','.join(fields)+' FROM '+table
                parameters=()
                if table=='tasks':
                    query+=" WHERE updated>? OR status IN ('queued','running','interrupted','cancel_requested','needs_reconciliation')"
                    parameters=(time.time()-max(1,keep_task_days)*86400,)
                for row in conn.execute(query,parameters):
                    for cell in row:
                        pending.append(json.loads(cell or '{}'))
    scanned=set()
    while pending:
        value=pending.pop()
        for text in _paths(value):
            normalized=text
            if '/data/' in text: normalized='data/'+text.split('/data/',1)[1]
            if normalized.startswith('data/'):
                path=(root/normalized).resolve()
                if root not in path.parents: continue
                references.add(path)
                parts=Path(normalized).parts
                if len(parts)>=3 and parts[1]=='ai_jobs':
                    job=root/'data/ai_jobs'/parts[2]
                    references.add(job)
                    if job not in scanned:
                        scanned.add(job)
                        for name in ('review_package.json','workflow_checkpoint.json'):
                            manifest=job/name
                            if manifest.exists(): pending.append(json.loads(manifest.read_text('utf-8')))
            elif re.fullmatch('[a-f0-9]{16}',text):
                job=root/'data/ai_jobs'/text
                if job.exists() and job not in scanned:
                    pending.append({'path':job.relative_to(root).as_posix()+'/review_package.json'})
    return references


def cleanup(root, *, days=30, apply=False):
    root=Path(root).resolve(); data=(root/'data').resolve()
    if root not in data.parents or (root/'data').is_symlink():
        raise ValueError('Unsafe data directory')
    references=live_references(root,days); before=time.time()-max(1,days)*86400; candidates=[]
    jobs=data/'ai_jobs'
    if jobs.exists():
        for job in jobs.iterdir():
            if job.is_dir() and not job.is_symlink() and re.fullmatch('[a-f0-9]{16}',job.name) and job.stat().st_mtime<before and not any(job==path or job in path.parents for path in references):
                candidates.append(job)
    assets=data/'products/assets'
    if assets.exists():
        for path in assets.iterdir():
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime<before and path not in references:
                candidates.append(path)
    cache=data/'ai_image_cache'
    if cache.exists():
        for path in cache.iterdir():
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime<before and path not in references:
                candidates.append(path)
    inputs=data/'task_inputs'
    if inputs.exists():
        for directory in inputs.iterdir():
            if directory.is_dir() and not directory.is_symlink() and re.fullmatch('[a-f0-9]{32}',directory.name) and directory.stat().st_mtime<before and not any(directory==path or directory in path.parents for path in references):
                candidates.append(directory)
    for path in candidates:
        final=path.resolve()
        if data not in final.parents or path.is_symlink(): raise ValueError('Unsafe cleanup path')
        if apply:
            shutil.rmtree(final) if final.is_dir() else final.unlink()
    return {'dry_run':not apply,'protected_references':len(references),'candidates':[str(path.relative_to(root)) for path in candidates]}


def backup(root, destination):
    root=Path(root).resolve(); destination=Path(destination).resolve()
    if destination.exists() or destination==root or root in destination.parents:
        raise ValueError('Backup must be a new directory outside the workspace')
    destination.mkdir(parents=True,mode=0o700)
    hashes={}
    # Backup application data only. Signing keys and session/provider secrets
    # are excluded; rental auth DB is an operator-controlled account backup.
    for source in (root/'data').rglob('*'):
        if not source.is_file() or source.is_symlink() or source.suffix in {'.log','.tmp'} or source.name.endswith(('-wal','-shm')) or source.name in {'license.json','usage.json'}:
            continue
        relative=source.relative_to(root); target=destination/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        if source.suffix=='.sqlite3':
            with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
        else:
            shutil.copy2(source,target)
        hashes[relative.as_posix()]=hashlib.sha256(target.read_bytes()).hexdigest()
    (destination/'backup_manifest.json').write_text(json.dumps({'created':time.time(),'files':hashes},indent=2),'utf-8')
    return {'files':len(hashes),'destination':str(destination)}


def restore_backup(source, destination):
    source=Path(source).resolve(); destination=Path(destination).resolve()
    if destination.exists(): raise ValueError('Restore only to a new empty directory')
    manifest=json.loads((source/'backup_manifest.json').read_text('utf-8'))
    checked=[]
    for relative,digest in manifest['files'].items():
        original=(source/relative).resolve(); target=(destination/relative).resolve()
        if source not in original.parents or destination not in target.parents or original.is_symlink() or hashlib.sha256(original.read_bytes()).hexdigest()!=digest:
            raise ValueError('Backup integrity verification failed')
        checked.append((original,target))
    for original,target in checked:
        target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(original,target)
    return {'files':len(checked),'destination':str(destination)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('action',choices=['cleanup','backup','restore']);parser.add_argument('root');parser.add_argument('--destination');parser.add_argument('--days',type=int,default=30);parser.add_argument('--apply',action='store_true'); args=parser.parse_args()
    result=cleanup(args.root,days=args.days,apply=args.apply) if args.action=='cleanup' else backup(args.root,args.destination) if args.action=='backup' else restore_backup(args.root,args.destination)
    print(json.dumps(result,ensure_ascii=True,indent=2))
