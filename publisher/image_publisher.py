"""
以图发品模块
通过上传图片，利用PDD的AI识别功能发布商品
"""
import asyncio
from typing import List, Optional, Dict
from pathlib import Path

from loguru import logger

import sys
sys.path.append(str(Path(__file__).parent.parent))
from browser.connector import ChromeConnector
from browser.navigator import PageNavigator
from browser.file_uploader import FileUploader
from publisher.form_filler import FormFiller
from utils.image_utils import ImageProcessor
from config import ACTION_DELAYS, DATA_DIR


class ImagePublisher:
    """以图发品发布器"""

    def __init__(self, connector: ChromeConnector = None):
        self.connector = connector
        self.navigator = None
        self.form_filler = None
        self.file_uploader = None
        self.image_processor = ImageProcessor()

    async def init(self, connector: ChromeConnector = None):
        """初始化发布器"""
        if connector:
            self.connector = connector
        elif not self.connector:
            self.connector = ChromeConnector()
            await self.connector.connect()

        self.navigator = PageNavigator(self.connector)
        self.form_filler = FormFiller(self.connector)
        self.file_uploader = FileUploader(self.connector)

    async def publish(
        self,
        images: List[str],
        title: str = None,
        category: str = None,
        price: float = None,
        stock: int = None,
        description: str = None,
        attributes: Dict = None,
        auto_submit: bool = False
    ) -> Dict:
        """
        以图发品

        Args:
            images: 图片路径列表
            title: 商品标题（可选，AI会自动识别）
            category: 商品分类（可选，AI会自动识别）
            price: 价格
            stock: 库存
            description: 商品描述
            attributes: 商品属性
            auto_submit: 是否自动提交（建议False，人工确认后再提交）

        Returns:
            发布结果
        """
        result = {
            "success": False,
            "message": "",
            "goods_id": None,
            "steps_completed": []
        }

        try:
            # 1. 初始化
            if not self.navigator:
                await self.init()

            # 2. 预处理图片
            logger.info("预处理图片...")
            processed_images = self.image_processor.batch_process(
                images,
                output_dir=DATA_DIR["images"]
            )

            if not processed_images:
                result["message"] = "没有有效的图片"
                return result

            # 3. 导航到发布页面
            logger.info("导航到发布页面...")
            await self.navigator.go_to_publish()
            await asyncio.sleep(ACTION_DELAYS["page_load"])

            # 4. 确保在以图发品Tab
            await self.navigator.switch_tab("以图发品")
            await asyncio.sleep(ACTION_DELAYS["click"])

            # 5. 上传图片
            logger.info("上传图片...")
            upload_success = await self._upload_images(processed_images)
            if not upload_success:
                result["message"] = "图片上传失败"
                return result
            result["steps_completed"].append("upload_images")

            # 6. 等待AI识别
            logger.info("等待AI识别商品信息...")
            await asyncio.sleep(ACTION_DELAYS["ai_recognize"])

            # 7. 确认/修改分类
            if category:
                logger.info(f"设置商品分类: {category}")
                await self._set_category(category)
            result["steps_completed"].append("set_category")

            # 8. 点击下一步
            logger.info("点击下一步...")
            await self.navigator.click_next_step()
            await asyncio.sleep(ACTION_DELAYS["page_load"])
            result["steps_completed"].append("next_step")

            # 9. 填写商品信息
            logger.info("填写商品信息...")
            form_data = {
                "title": title,
                "price": price,
                "stock": stock,
                "description": description,
                "attributes": attributes or {}
            }
            form_success = await self.form_filler.fill_form(form_data)
            if not form_success:
                result["message"] = "表单填写失败"
                return result
            result["steps_completed"].append("fill_form")

            # 10. 截图保存
            screenshot_path = await self.navigator.take_screenshot("before_submit")
            result["screenshot"] = screenshot_path

            # 11. 提交（如果启用自动提交）
            if auto_submit:
                logger.info("自动提交...")
                submit_success = await self.navigator.click_submit()
                if submit_success:
                    result["success"] = True
                    result["message"] = "商品发布成功"
                    result["steps_completed"].append("submit")
                else:
                    result["message"] = "提交失败"
            else:
                result["success"] = True
                result["message"] = "商品信息已填写完成，请手动确认后提交"
                logger.info("请手动确认商品信息后点击提交按钮")

            return result

        except Exception as e:
            logger.error(f"发布商品失败: {e}")
            result["message"] = f"发布失败: {str(e)}"
            return result

    async def _upload_images(self, image_paths: List[str]) -> bool:
        """
        上传图片

        Args:
            image_paths: 图片路径列表

        Returns:
            是否成功
        """
        try:
            # 使用FileUploader上传图片
            success = await self.file_uploader.upload_to_image_input(image_paths)

            if success:
                # 等待上传完成
                await self.file_uploader.wait_for_upload_complete(timeout=15)
                return True

            return False

        except Exception as e:
            logger.error(f"上传图片失败: {e}")
            return False

    async def _set_category(self, category: str) -> bool:
        """
        设置商品分类

        Args:
            category: 分类名称

        Returns:
            是否成功
        """
        try:
            # 等待分类选择区域出现
            await asyncio.sleep(2)

            # 查找分类选择器
            result = await self.connector.evaluate(f"""
                (() => {{
                    // 查找分类选择相关的元素
                    const elements = document.querySelectorAll('[class*="category"], [class*="select"]');
                    for (const el of elements) {{
                        if (el.innerText.includes('{category}')) {{
                            el.click();
                            return true;
                        }}
                    }}

                    // 尝试点击下拉框
                    const dropdowns = document.querySelectorAll('[class*="dropdown"], [class*="select"]');
                    for (const dropdown of dropdowns) {{
                        if (dropdown.innerText.includes('选择') || dropdown.innerText.includes('分类')) {{
                            dropdown.click();
                            return 'clicked_dropdown';
                        }}
                    }}

                    return false;
                }})()
            """)

            if result == 'clicked_dropdown':
                # 等待下拉框展开
                await asyncio.sleep(1)

                # 选择分类
                await self.connector.evaluate(f"""
                    (() => {{
                        const options = document.querySelectorAll('[class*="option"], [class*="item"]');
                        for (const option of options) {{
                            if (option.innerText.includes('{category}')) {{
                                option.click();
                                return true;
                            }}
                        }}
                        return false;
                    }})()
                """)

            await asyncio.sleep(ACTION_DELAYS["click"])
            return True

        except Exception as e:
            logger.error(f"设置分类失败: {e}")
            return False

    async def preview(self) -> str:
        """
        预览当前填写的商品信息

        Returns:
            截图路径
        """
        return await self.navigator.take_screenshot("preview")

    async def save_draft(self) -> bool:
        """保存草稿"""
        try:
            result = await self.connector.evaluate("""
                (() => {
                    const buttons = document.querySelectorAll('button');
                    for (const btn of buttons) {
                        if (btn.innerText.includes('保存') || btn.innerText.includes('草稿')) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                })()
            """)
            return result
        except Exception as e:
            logger.error(f"保存草稿失败: {e}")
            return False
