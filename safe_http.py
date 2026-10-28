"""Bounded outbound HTTP with DNS/IP validation and connection pinning.

Provider credentials are never included in error text.  Redirects are
revalidated and authorization is never forwarded to another origin.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
from contextvars import ContextVar
from urllib.parse import urljoin, urlsplit


class OutboundError(ValueError):
    pass


OUTBOUND_PRODUCTION = ContextVar('outbound_production', default=False)


def production_mode():
    return OUTBOUND_PRODUCTION.get() or os.getenv('PDD_DEPLOYMENT', 'local').lower() == 'rental'


def private_api_enabled() -> bool:
    return not production_mode() and (
        os.getenv("PDD_AI_DEMO", "").lower() in {"1", "true", "yes"}
        or os.getenv("PDD_ALLOW_PRIVATE_API_URL", "").lower() in {"1", "true", "yes"}
    )


def validate_public_url(url: str, *, allow_private: bool = False, resolve_dns: bool = True, require_https: bool | None = None) -> tuple[str, int, list[str]]:
    try:
        parts = urlsplit(str(url).strip())
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except (ValueError, TypeError) as exc:
        raise OutboundError("服务地址格式不正确") from exc
    production = production_mode()
    if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password or parts.fragment:
        raise OutboundError("仅支持不含用户密码或片段的HTTP(S)地址")
    if (production if require_https is None else require_https) and parts.scheme != "https":
        raise OutboundError("网站环境只允许HTTPS服务地址")
    if any(char in str(url) for char in ("\r", "\n", "\x00")):
        raise OutboundError("服务地址包含无效字符")
    if not 1 <= port <= 65535:
        raise OutboundError("服务端口无效")
    allow_private = bool(allow_private and not production)
    if not allow_private and (host in {"localhost", "metadata", "metadata.google.internal"} or host.endswith((".local", ".localhost", ".internal"))):
        raise OutboundError("不允许访问本机、内网或云元数据地址")
    addresses: list[str] = []
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        if resolve_dns:
            try:
                addresses = sorted({record[4][0].split("%")[0] for record in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
            except OSError as exc:
                raise OutboundError("无法解析服务域名，请检查地址和网络") from exc
    for address in addresses:
        try:
            value = ipaddress.ip_address(address)
            mapped = getattr(value, "ipv4_mapped", None)
            if not allow_private and (not value.is_global or (mapped is not None and not mapped.is_global)):
                raise OutboundError("服务域名解析到本机、内网或不可路由地址")
        except ValueError as exc:
            raise OutboundError("服务域名返回了无效地址") from exc
    if resolve_dns and not addresses:
        raise OutboundError("服务域名没有可连接地址")
    return host, port, addresses


def _send(method: str, url: str, *, headers: dict, body: bytes | str | None, timeout: int, max_bytes: int, allow_private: bool) -> tuple[int, dict, bytes]:
    """Transport seam used by offline tests; socket dials validated IP only."""
    host, port, addresses = validate_public_url(url, allow_private=allow_private)
    parts = urlsplit(url)
    selected_ip = addresses[0]
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())

        def connect():
            plain = socket.create_connection((selected_ip, port), timeout=timeout)
            connection.sock = connection._context.wrap_socket(plain, server_hostname=host)
        connection.connect = connect
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.connect = lambda: setattr(connection, "sock", socket.create_connection((selected_ip, port), timeout=timeout))
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        connection.request(method.upper(), path, body=body, headers=headers)
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        try:
            declared = int(response_headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > max_bytes:
            raise OutboundError("远程响应超过允许大小")
        chunks = []
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise OutboundError("远程响应超过允许大小")
            chunks.append(chunk)
        return response.status, response_headers, b"".join(chunks)
    except OutboundError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise OutboundError("远程请求失败或超时，请核对服务商记录后再重试") from exc
    finally:
        connection.close()


def request_bytes(method: str, url: str, *, key: str = "", json_payload=None, data=None, files=None, timeout: int = 180, max_bytes: int = 48 * 1024 * 1024, allow_private: bool = False, headers: dict | None = None, max_redirects: int = 3) -> tuple[bytes, dict]:
    request_headers = {"User-Agent": "PDD-Image-Workbench/2", "Accept-Encoding": "identity", **(headers or {})}
    if key:
        request_headers["Authorization"] = "Bearer " + key
    body = None
    if files is not None:
        # Preparing a multipart message performs no network activity.
        import requests
        prepared = requests.Request(method, url, data=data, files=files).prepare()
        body = prepared.body
        request_headers.update({key: value for key, value in prepared.headers.items() if key.lower() != "authorization"})
    elif json_payload is not None:
        body = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    elif data is not None:
        from urllib.parse import urlencode
        body = urlencode(data).encode("utf-8") if isinstance(data, dict) else data
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body is not None and len(body) > 64 * 1024 * 1024:
        raise OutboundError("图片请求超过允许大小")
    current = url
    for redirect in range(max_redirects + 1):
        status, response_headers, raw = _send(method, current, headers=request_headers, body=body, timeout=max(5, min(timeout, 900)), max_bytes=max_bytes, allow_private=allow_private)
        if status in {301, 302, 303, 307, 308}:
            if method.upper() not in {"GET", "HEAD"}:
                raise OutboundError("服务接口发生重定向，请直接填写最终API地址，避免重复提交")
            if redirect >= max_redirects or not response_headers.get("location"):
                raise OutboundError("远程跳转次数过多")
            target = urljoin(current, response_headers["location"])
            validate_public_url(target, allow_private=allow_private)
            if (urlsplit(current).scheme, urlsplit(current).netloc) != (urlsplit(target).scheme, urlsplit(target).netloc):
                request_headers = {key:value for key,value in request_headers.items() if key.lower() not in {'authorization','cookie'}}
            current = target
            continue
        if not 200 <= status < 300:
            # Do not echo provider response bodies (can contain keys/prompt).
            raise OutboundError(f"服务接口返回HTTP {status}" + ("，请稍后重试" if status == 429 else ""))
        return raw, response_headers
    raise OutboundError("远程跳转失败")


def request_json(method: str, url: str, *, key: str = "", json_payload=None, data=None, files=None, timeout: int = 180, max_bytes: int = 48 * 1024 * 1024, allow_private: bool = False):
    raw, _ = request_bytes(method, url, key=key, json_payload=json_payload, data=data, files=files, timeout=timeout, max_bytes=max_bytes, allow_private=allow_private)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OutboundError("服务接口没有返回有效JSON") from exc
    if not isinstance(result, dict):
        raise OutboundError("服务接口返回的JSON格式不正确")
    return result


def download_bytes(url: str, timeout: int = 60, max_bytes: int = 24 * 1024 * 1024, allow_private: bool = False, headers: dict | None = None) -> bytes:
    raw, _ = request_bytes("GET", url, timeout=timeout, max_bytes=max_bytes, allow_private=allow_private, headers=headers)
    return raw


async def read_upload(upload, max_bytes: int, *, chunk_bytes: int = 64 * 1024) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = await upload.read(chunk_bytes)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise OutboundError("上传文件超过允许大小")
        chunks.append(chunk)
    return b"".join(chunks)
