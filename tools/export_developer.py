"""Export a new, audited source tree without runtime data or production keys."""
import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    'admin_core.py', 'ai_api.py', 'ai_review.py', 'ai_service.py',
    'ai_workflow.py', 'asset_store.py', 'core_planner.py', 'creative_proposal.py',
    'image_recrop.py', 'launcher.py', 'launcher_admin.py', 'license.py',
    'listing_readback.py', 'local_drafts.py', 'local_security.py',
    'maintenance.py', 'one_click.py', 'one_click_list.py', 'product_package.py',
    'rental_server.py', 'resource_limits.py', 'run_local.py', 'run_dev.py',
    'safe_http.py', 'size_chart.py', 'skill_templates.py', 'sku_workbook.py',
    'task_store.py', 'verify_zip.py', 'workflow_engine.py',
    'requirements.txt', 'requirements.lock.txt', 'skill_template.lock.json',
    'README.md', 'DEVELOPMENT.md', 'HANDOFF_STATUS.md', 'THIRD_PARTY_NOTICES.md',
    'WORKFLOW_20261028.md', 'IMPLEMENTATION_20261027.md', 'HANDOFF_VALIDATION.md',
    '.gitignore', '.env.example',
}
FOLDERS = ('web', 'tests', 'server', 'browser', 'extractor', 'publisher', 'assets_src',
           'scripts', 'utils', 'vendor/ecom-details-image')
EXTENSIONS = {'.py', '.js', '.css', '.html', '.json', '.md', '.txt',
              '.png', '.jpg', '.jpeg', '.webp'}
TOOLS = ('release_workbench.py', 'extract_assets.py', 'enc_assets.py',
         'export_developer.py')
SECRET_RULES = {
    'private-key-material': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{40,}'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'api-token': re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b'),
    'url-credentials': re.compile(r'https?://[^\s/<>:@]+:[^\s/<>@]+@'),
}


def audit(root):
    root = Path(root)
    issues = []
    for path in root.rglob('*'):
        if not path.is_file() or '.git' in path.relative_to(root).parts:
            continue
        relative = path.relative_to(root).as_posix()
        parts = path.relative_to(root).parts
        if (path.is_symlink() or any(p.startswith(('browser_data', '.venv', '__pycache__')) for p in parts)
                or parts[0] in {'data', 'keygen', 'packs', 'validation', '.release_previous'}
                or path.suffix.lower() in {'.pem', '.key', '.p12', '.pfx', '.db', '.sqlite', '.sqlite3', '.zip', '.pyc', '.log'}
                or path.name.startswith('.env') and path.name != '.env.example'):
            issues.append({'file': relative, 'rule': 'excluded-file'})
        if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}:
            continue
        content = path.read_text('utf-8', errors='replace')
        for name, pattern in SECRET_RULES.items():
            for match in pattern.finditer(content):
                issues.append({'file': relative, 'rule': name,
                               'line': content.count('\n', 0, match.start()) + 1})
    return issues


def export(destination, legacy_archive=None):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Destination must be a new directory; existing checkouts are never overwritten')
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError('Use a separate source directory outside the working project')
    files = [ROOT/name for name in ROOT_FILES if (ROOT/name).is_file()]
    for folder in FOLDERS:
        files.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and not p.is_symlink()
                     and not any(part in {'.git', '__pycache__'} for part in p.relative_to(ROOT).parts)
                     and (p.suffix.lower() in EXTENSIONS or p.name in {'LICENSE', 'NOTICE', '.gitignore', '.env.example'}))
    files.extend(ROOT/'tools'/name for name in TOOLS)
    destination.mkdir(parents=True)
    for path in sorted(set(files)):
        target = destination/path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # These are editable browser scripts, not credentials. Recover just the
    # known source folder from the original archive; never extract keygen/data.
    if legacy_archive:
        with zipfile.ZipFile(legacy_archive) as archive:
            for item in archive.infolist():
                path = Path(item.filename)
                if (item.is_dir() or len(path.parts) != 2 or path.parts[0] != 'assets_src'
                        or path.suffix not in {'.txt', '.json'}):
                    continue
                if item.file_size > 2 * 1024 * 1024 or item.compress_size and item.file_size/item.compress_size > 100:
                    raise ValueError('Unexpected source asset size')
                target = destination/path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))
    findings = audit(destination)
    if findings:
        raise ValueError(json.dumps({'audit_failed': findings}, ensure_ascii=True))
    manifest = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(destination.rglob('*')) if p.is_file()}
    (destination/'SOURCE_MANIFEST.json').write_text(json.dumps({
        'schema_version': 1, 'date': '2026-10-28', 'files': manifest,
        'excluded': ['runtime data', 'production credentials', 'browser profiles', 'old archives', 'one-off probes'],
    }, indent=2), 'utf-8')
    return {'destination': str(destination), 'files': len(manifest)+1,
            'source_assets': len(list((destination/'assets_src').glob('*.txt'))),
            'audit_findings': findings}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination')
    parser.add_argument('--legacy-archive')
    parser.add_argument('--audit')
    args = parser.parse_args()
    if args.audit:
        findings = audit(args.audit)
        print(json.dumps({'audit_findings': findings}, ensure_ascii=True))
        raise SystemExit(bool(findings))
    if not args.destination:
        parser.error('--destination or --audit is required')
    print(json.dumps(export(args.destination, args.legacy_archive), ensure_ascii=True))
