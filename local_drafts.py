"""Immutable content-addressed images and complete, owner-scoped drafts."""
import hashlib
import io
import json
import os
import shutil
from pathlib import Path

from PIL import Image

from product_package import normalize_product_package
from safe_http import download_bytes
from task_store import StoreError

MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_ASSET_BYTES = 2 * 1024 ** 3


def check_capacity(root: Path, incoming=0):
    data = Path(root) / 'data'
    data.mkdir(parents=True, exist_ok=True)
    limit = int(os.environ.get('PDD_ASSET_LIMIT_BYTES', MAX_ASSET_BYTES))
    used = sum(p.stat().st_size for p in data.rglob('*') if p.is_file())
    if used + incoming > limit or shutil.disk_usage(data).free < incoming + 128 * 1024 ** 2:
        raise StoreError('图片存储空间不足，请清理不再使用的商品版本')


def freeze_image(root: Path, value: str) -> str:
    root = Path(root).resolve()
    if value.startswith(('https://', 'http://')):
        raw = download_bytes(value, max_bytes=MAX_IMAGE_BYTES)
    else:
        path = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        if (root / 'data').resolve() not in path.parents or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise StoreError('图片丢失、过大或不属于当前商品，请重新生成或上传')
        if path.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}:
            raise StoreError('只允许保存真实图片文件')
        raw = path.read_bytes()
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.width * im.height > MAX_PIXELS or im.format not in {'JPEG', 'PNG', 'WEBP', 'BMP'}:
                raise ValueError('invalid image')
            im.verify()
            ext = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp', 'BMP': 'bmp'}[im.format]
    except Exception as exc:
        raise StoreError('图片格式损坏或像素过大，请替换后保存') from exc
    digest = hashlib.sha256(raw).hexdigest()
    destination = root / 'data' / 'products' / 'assets' / (digest + '.' + ext)
    if not destination.exists():
        check_capacity(root, len(raw))
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.tmp-' + os.urandom(4).hex())
        temporary.write_bytes(raw)
        os.replace(temporary, destination)
    return destination.relative_to(root).as_posix()


def save_package(root, store, owner, raw):
    existing = store.get_draft(owner, str(raw.get('id') or ''))
    package = normalize_product_package(raw, existing)
    count = len(package['main_images']) + len(package['detail_images']) + len(package['spec_images'])
    if count > 150:
        raise StoreError('单商品图片超过150张，请减少图片后保存')
    mapping = {}
    def freeze(value):
        if value not in mapping:
            mapping[value] = freeze_image(Path(root), str(value))
        return mapping[value]
    package['main_images'] = [freeze(value) for value in package['main_images']]
    package['detail_images'] = [freeze(value) for value in package['detail_images']]
    package['spec_images'] = {key: freeze(value) for key, value in package['spec_images'].items() if value}
    for module in package['module_outputs']:
        if module.get('path') in mapping:
            module['path'] = mapping[module['path']]
    package['revision'] = int((existing or {}).get('revision') or package.get('revision') or 0) + 1
    return store.save_draft(owner, package)


def import_legacy(root, store, owner):
    path = Path(root) / 'data' / 'migrated_products.json'
    if not path.exists():
        return
    try:
        rows = json.loads(path.read_text('utf-8'))
    except Exception as exc:
        raise StoreError('旧商品列表损坏，已保留原文件，请先恢复备份') from exc
    for row in rows:
        if isinstance(row, dict) and row.get('id') and not store.get_draft(owner, row['id']):
            # Import metadata only. Existing image paths continue to work and
            # become immutable on the next successful save.
            store.save_draft(owner, normalize_product_package(row))
