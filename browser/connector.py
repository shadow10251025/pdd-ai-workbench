"""
Chrome CDP连接器
通过Chrome DevTools Protocol连接已打开的Chrome浏览器
"""
import asyncio
import json
import base64
from typing import Optional, Dict, Any, List
from pathlib import Path

try:
    import websockets
except ImportError:
    raise ImportError("请安装websockets: pip install websockets")

from loguru import logger

import sys
sys.path.append(str(Path(__file__).parent.parent))
from config import CHROME_CONFIG


class ChromeConnector:
    """Chrome CDP连接器"""

    def __init__(self, host: str = None, port: int = None):
        self.host = host or CHROME_CONFIG["host"]
        self.port = port or CHROME_CONFIG["debug_port"]
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._msg_id = 0
        self._connected = False

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def connect(self, page_id: str = None) -> bool:
        """
        连接到Chrome浏览器

        Args:
            page_id: 指定页面ID，如果不指定则连接到第一个页面

        Returns:
            连接是否成功
        """
        try:
            # 获取页面列表
            import requests
            resp = requests.get(f"{self.base_url}/json", timeout=5)
            pages = resp.json()

            if not pages:
                logger.error("没有找到打开的页面")
                return False

            # 选择要连接的页面
            if page_id:
                target = next((p for p in pages if p["id"] == page_id), None)
                if not target:
                    logger.error(f"未找到页面: {page_id}")
                    return False
            else:
                # 优先选择拼多多页面
                target = None
                for p in pages:
                    if "pinduoduo.com" in p.get("url", ""):
                        target = p
                        break
                if not target:
                    target = pages[0]

            ws_url = target["webSocketDebuggerUrl"]
            logger.info(f"连接到页面: {target['title']} ({target['url']})")

            # 建立WebSocket连接
            self.ws = await websockets.connect(
                ws_url,
                max_size=10 * 1024 * 1024,  # 10MB
                ping_interval=30,
                ping_timeout=10
            )
            self._connected = True

            # 启用必要的CDP域
            await self.send_cmd("Page.enable")
            await self.send_cmd("Runtime.enable")
            await self.send_cmd("DOM.enable")

            logger.info("Chrome连接成功")
            return True

        except Exception as e:
            logger.error(f"连接Chrome失败: {e}")
            self._connected = False
            return False

    async def disconnect(self):
        """断开连接"""
        if self.ws:
            await self.ws.close()
            self._connected = False
            logger.info("Chrome连接已断开")

    async def send_cmd(self, method: str, params: Dict = None) -> Dict[str, Any]:
        """
        发送CDP命令

        Args:
            method: CDP方法名
            params: 参数

        Returns:
            响应结果
        """
        if not self._connected or not self.ws:
            raise ConnectionError("未连接到Chrome")

        self._msg_id += 1
        cmd = {"id": self._msg_id, "method": method}
        if params:
            cmd["params"] = params

        await self.ws.send(json.dumps(cmd))

        # 等待响应
        while True:
            try:
                resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
                if resp.get("id") == self._msg_id:
                    if "error" in resp:
                        logger.error(f"CDP错误: {resp['error']}")
                    return resp
                # 忽略事件消息
            except asyncio.TimeoutError:
                logger.warning("等待CDP响应超时")
                return {"error": "timeout"}

    async def evaluate(self, expression: str) -> Any:
        """
        执行JavaScript表达式

        Args:
            expression: JavaScript表达式

        Returns:
            执行结果
        """
        result = await self.send_cmd("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        })

        if "result" in result and "result" in result["result"]:
            return result["result"]["result"].get("value")
        return None

    async def screenshot(self, save_path: str = None, full_page: bool = False) -> str:
        """
        截取当前页面截图

        Args:
            save_path: 保存路径
            full_page: 是否截取完整页面

        Returns:
            截图保存路径
        """
        params = {"format": "png"}
        if full_page:
            # 获取页面完整尺寸
            layout = await self.send_cmd("Page.getLayoutMetrics")
            if "result" in layout:
                content = layout["result"].get("contentSize", {})
                params["clip"] = {
                    "x": 0,
                    "y": 0,
                    "width": content.get("width", 1920),
                    "height": content.get("height", 1080),
                    "scale": 1
                }

        result = await self.send_cmd("Page.captureScreenshot", params)

        if "result" in result:
            img_data = base64.b64decode(result["result"]["data"])

            if not save_path:
                save_path = f"screenshot_{self._msg_id}.png"

            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(img_data)

            logger.debug(f"截图已保存: {save_path}")
            return save_path

        return None

    async def get_page_url(self) -> str:
        """获取当前页面URL"""
        return await self.evaluate("window.location.href")

    async def get_page_title(self) -> str:
        """获取当前页面标题"""
        return await self.evaluate("document.title")

    async def get_page_text(self, max_length: int = 5000) -> str:
        """获取页面文本内容"""
        return await self.evaluate(f"document.body.innerText.substring(0, {max_length})")

    async def query_selector(self, selector: str) -> Optional[Dict]:
        """
        查询DOM元素

        Args:
            selector: CSS选择器

        Returns:
            元素信息
        """
        result = await self.send_cmd("Runtime.evaluate", {
            "expression": f"""
                (() => {{
                    const el = document.querySelector('{selector}');
                    if (!el) return null;
                    const rect = el.getBoundingClientRect();
                    return {{
                        tagName: el.tagName,
                        className: el.className,
                        id: el.id,
                        text: el.innerText?.substring(0, 200),
                        visible: rect.width > 0 && rect.height > 0,
                        rect: {{x: rect.x, y: rect.y, width: rect.width, height: rect.height}}
                    }};
                }})()
            """,
            "returnByValue": True
        })

        if "result" in result and "result" in result["result"]:
            return result["result"]["result"].get("value")
        return None

    async def query_selector_all(self, selector: str) -> List[Dict]:
        """
        查询所有匹配的DOM元素

        Args:
            selector: CSS选择器

        Returns:
            元素信息列表
        """
        result = await self.send_cmd("Runtime.evaluate", {
            "expression": f"""
                (() => {{
                    const elements = document.querySelectorAll('{selector}');
                    return Array.from(elements).map(el => {{
                        const rect = el.getBoundingClientRect();
                        return {{
                            tagName: el.tagName,
                            className: el.className?.substring(0, 100),
                            id: el.id,
                            text: el.innerText?.substring(0, 200),
                            visible: rect.width > 0 && rect.height > 0,
                            type: el.type || '',
                            placeholder: el.placeholder || '',
                            value: el.value || ''
                        }};
                    }});
                }})()
            """,
            "returnByValue": True
        })

        if "result" in result and "result" in result["result"]:
            return result["result"]["result"].get("value", [])
        return []

    async def click(self, selector: str) -> bool:
        """
        点击元素

        Args:
            selector: CSS选择器

        Returns:
            是否成功
        """
        result = await self.send_cmd("Runtime.evaluate", {
            "expression": f"""
                (() => {{
                    const el = document.querySelector('{selector}');
                    if (!el) return false;
                    el.click();
                    return true;
                }})()
            """,
            "returnByValue": True
        })

        if "result" in result and "result" in result["result"]:
            return result["result"]["result"].get("value", False)
        return False

    async def input_text(self, selector: str, text: str) -> bool:
        """
        在输入框中输入文本

        Args:
            selector: CSS选择器
            text: 要输入的文本

        Returns:
            是否成功
        """
        # 先聚焦
        await self.send_cmd("Runtime.evaluate", {
            "expression": f"""
                (() => {{
                    const el = document.querySelector('{selector}');
                    if (!el) return false;
                    el.focus();
                    el.value = '';
                    return true;
                }})()
            """,
            "returnByValue": True
        })

        # 逐字符输入
        for char in text:
            await self.send_cmd("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "text": char
            })
            await self.send_cmd("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "text": char
            })
            await asyncio.sleep(0.02)

        return True

    async def upload_file(self, selector: str, file_paths: List[str]) -> bool:
        """
        上传文件

        Args:
            selector: 文件输入框选择器
            file_paths: 文件路径列表

        Returns:
            是否成功
        """
        # 设置文件
        result = await self.send_cmd("Runtime.evaluate", {
            "expression": f"""
                (() => {{
                    const input = document.querySelector('{selector}');
                    if (!input) return false;
                    // 创建DataTransfer来设置文件
                    return true;
                }})()
            """,
            "returnByValue": True
        })

        # 使用DOM.setFileInputFiles方法
        # 需要先获取节点ID
        doc = await self.send_cmd("DOM.getDocument")
        if "result" in doc:
            node = await self.send_cmd("DOM.querySelector", {
                "nodeId": doc["result"]["root"]["nodeId"],
                "selector": selector
            })
            if "result" in node:
                await self.send_cmd("DOM.setFileInputFiles", {
                    "nodeId": node["result"]["nodeId"],
                    "files": file_paths
                })
                logger.info(f"已上传文件: {file_paths}")
                return True

        return False

    async def wait_for_element(self, selector: str, timeout: int = 10000) -> bool:
        """
        等待元素出现

        Args:
            selector: CSS选择器
            timeout: 超时时间（毫秒）

        Returns:
            元素是否出现
        """
        start = asyncio.get_event_loop().time()
        timeout_sec = timeout / 1000

        while (asyncio.get_event_loop().time() - start) < timeout_sec:
            el = await self.query_selector(selector)
            if el and el.get("visible"):
                return True
            await asyncio.sleep(0.5)

        return False

    async def scroll_to_bottom(self):
        """滚动到页面底部"""
        await self.evaluate("window.scrollTo(0, document.body.scrollHeight)")

    async def navigate(self, url: str) -> bool:
        """
        导航到指定URL

        Args:
            url: 目标URL

        Returns:
            是否成功
        """
        result = await self.send_cmd("Page.navigate", {"url": url})
        if "result" in result:
            # 等待页面加载
            await asyncio.sleep(2)
            return True
        return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        asyncio.get_event_loop().run_until_complete(self.disconnect())
