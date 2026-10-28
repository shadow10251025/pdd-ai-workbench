import io
import zipfile


def validate_workbook(data):
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('Excel文件超过8MB限制')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 300 or sum(entry.file_size for entry in entries) > 32 * 1024 * 1024:
            raise ValueError('Excel解压量或条目超过限制')
        for entry in entries:
            if entry.file_size > 16 * 1024 * 1024 or entry.file_size > max(1,entry.compress_size) * 1000:
                raise ValueError('Excel单文件解压量或压缩比例超过限制')
            if '..' in entry.filename.replace('\\','/').split('/') or entry.filename.startswith(('/', '\\')):
                raise ValueError('Excel包含无效路径')
