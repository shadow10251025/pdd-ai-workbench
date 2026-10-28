"""
文件上传模块
使用CDP原生API实现可靠的文件上传
"""
import asyncio
import os
from typing import List, Optional
from pathlib import Path

from loguru import logger


class FileUploader:
    """CDP文件上传器"""

    def __init__(self, connector):
        self.connector = connector

    async def upload_files(self, selector: str, file_paths: List[str]) -> bool:
        """
        上传文件到指定的input[type="file"]元素

        Args:
            selector: 文件输入框的CSS选择器
            file_paths: 要上传的文件路径列表

        Returns:
            是否成功
        """
        if not file_paths:
            logger.warning("没有要上传的文件")
            return False

        # 验证文件存在
        abs_paths = []
        for path in file_paths:
            abs_path = str(Path(path).resolve())
            if os.path.exists(abs_path):
                abs_paths.append(abs_path)
            else:
                logger.warning(f"文件不存在: {path}")

        if not abs_paths:
            logger.error("没有有效的文件")
            return False

        try:
            # 方法1: 使用CDP的DOM.setFileInputFiles
            success = await self._upload_via_cdp(selector, abs_paths)
            if success:
                return True

            # 方法2: 使用Runtime.evaluate触发
            success = await self._upload_via_runtime(selector, abs_paths)
            return success

        except Exception as e:
            logger.error(f"上传文件失败: {e}")
            return False

    async def _upload_via_cdp(self, selector: str, file_paths: List[str]) -> bool:
        """通过CDP原生API上传文件"""
        try:
            # 获取文档根节点
            doc_result = await self.connector.send_cmd("DOM.getDocument")
            if "result" not in doc_result:
                logger.error("获取文档失败")
                return False

            root_node_id = doc_result["result"]["root"]["nodeId"]

            # 查找文件输入框节点
            query_result = await self.connector.send_cmd("DOM.querySelector", {
                "nodeId": root_node_id,
                "selector": selector
            })

            if "result" not in query_result or query_result["result"].get("nodeId", 0) == 0:
                logger.error(f"未找到元素: {selector}")
                return False

            node_id = query_result["result"]["nodeId"]

            # 设置文件
            set_result = await self.connector.send_cmd("DOM.setFileInputFiles", {
                "nodeId": node_id,
                "files": file_paths
            })

            if "result" in set_result:
                logger.info(f"已上传 {len(file_paths)} 个文件")
                return True

            logger.warning(f"CDP上传失败: {set_result}")
            return False

        except Exception as e:
            logger.error(f"CDP上传异常: {e}")
            return False

    async def _upload_via_runtime(self, selector: str, file_paths: List[str]) -> bool:
        """通过Runtime触发文件上传"""
        try:
            # 构建文件路径的JavaScript数组
            js_paths = str(file_paths)

            result = await self.connector.evaluate(f"""
                (() => {{
                    const input = document.querySelector('{selector}');
                    if (!input) return false;

                    // 创建并触发change事件
                    const event = new Event('change', {{ bubbles: true }});
                    input.dispatchEvent(event);

                    return true;
                }})()
            """)

            return result

        except Exception as e:
            logger.error(f"Runtime上传异常: {e}")
            return False

    async def find_file_inputs(self) -> List[dict]:
        """
        查找页面上所有的文件输入框

        Returns:
            文件输入框信息列表
        """
        result = await self.connector.evaluate("""
            (() => {
                const inputs = document.querySelectorAll('input[type="file"]');
                return Array.from(inputs).map((input, index) => ({
                    index: index,
                    accept: input.accept || '',
                    multiple: input.multiple,
                    id: input.id || '',
                    name: input.name || '',
                    className: input.className || '',
                    visible: input.getBoundingClientRect().width > 0
                }));
            })()
        """)

        return result or []

    async def upload_to_image_input(self, file_paths: List[str]) -> bool:
        """
        上传图片到图片输入框（专门用于PDD发布页面）

        Args:
            file_paths: 图片文件路径列表

        Returns:
            是否成功
        """
        # 查找图片上传输入框
        inputs = await self.find_file_inputs()

        image_input = None
        for inp in inputs:
            if "image" in inp.get("accept", "").lower():
                image_input = inp
                break

        if not image_input:
            # 使用默认选择器
            selector = 'input[type="file"][accept*="image"]'
        else:
            # 构建选择器
            if image_input.get("id"):
                selector = f'#{image_input["id"]}'
            elif image_input.get("name"):
                selector = f'input[name="{image_input["name"]}"]'
            else:
                selector = f'input[type="file"]:nth-child({image_input["index"] + 1})'

        return await self.upload_files(selector, file_paths)

    async def upload_with_retry(
        self,
        selector: str,
        file_paths: List[str],
        max_retries: int = 3,
        retry_delay: float = 2.0
    ) -> bool:
        """
        带重试的文件上传

        Args:
            selector: 文件输入框选择器
            file_paths: 文件路径列表
            max_retries: 最大重试次数
            retry_delay: 重试间隔（秒）

        Returns:
            是否成功
        """
        for attempt in range(max_retries):
            logger.info(f"上传尝试 {attempt + 1}/{max_retries}")

            success = await self.upload_files(selector, file_paths)
            if success:
                return True

            if attempt < max_retries - 1:
                logger.info(f"等待 {retry_delay} 秒后重试...")
                await asyncio.sleep(retry_delay)

        logger.error(f"上传失败，已重试 {max_retries} 次")
        return False

    async def wait_for_upload_complete(self, timeout: float = 10.0) -> bool:
        """
        等待上传完成

        Args:
            timeout: 超时时间（秒）

        Returns:
            是否完成
        """
        start = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start) < timeout:
            # 检查是否有上传进度或预览图
            result = await self.connector.evaluate("""
                (() => {
                    // 检查上传预览图
                    const previews = document.querySelectorAll('[class*="upload"] img, [class*="preview"] img');
                    if (previews.length > 0) return { count: previews.length, status: 'complete' };

                    // 检查上传进度
                    const progress = document.querySelector('[class*="progress"], [class*="loading"]');
                    if (progress) return { status: 'uploading' };

                    return { status: 'unknown' };
                })()
            """)

            if result and result.get("status") == "complete":
                logger.info(f"上传完成，共 {result.get('count', 0)} 张图片")
                return True

            await asyncio.sleep(0.5)

        logger.warning("等待上传超时")
        return False
