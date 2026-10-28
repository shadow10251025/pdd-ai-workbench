"""
页面导航器
封装拼多多商家后台的页面导航逻辑
"""
import asyncio
from typing import Optional
from pathlib import Path

from loguru import logger

import sys
sys.path.append(str(Path(__file__).parent.parent))
from browser.connector import ChromeConnector
from config import PDD_URLS, ACTION_DELAYS


class PageNavigator:
    """拼多多商家后台页面导航器"""

    def __init__(self, connector: ChromeConnector):
        self.connector = connector

    async def go_to_goods_list(self) -> bool:
        """导航到商品列表页"""
        logger.info("导航到商品列表页")
        success = await self.connector.navigate(PDD_URLS["goods_list"])
        if success:
            await asyncio.sleep(ACTION_DELAYS["page_load"])
        return success

    async def go_to_publish(self) -> bool:
        """导航到发布新商品页"""
        logger.info("导航到发布新商品页")
        success = await self.connector.navigate(PDD_URLS["publish"])
        if success:
            await asyncio.sleep(ACTION_DELAYS["page_load"])
        return success

    async def go_to_goods_edit(self, goods_id: str) -> bool:
        """
        导航到商品编辑页

        Args:
            goods_id: 商品ID
        """
        url = f"{PDD_URLS['goods_edit']}?goods_id={goods_id}"
        logger.info(f"导航到商品编辑页: {goods_id}")
        success = await self.connector.navigate(url)
        if success:
            await asyncio.sleep(ACTION_DELAYS["page_load"])
        return success

    async def is_on_page(self, page_type: str) -> bool:
        """
        检查当前是否在指定页面

        Args:
            page_type: 页面类型 (goods_list, publish, goods_edit)
        """
        current_url = await self.connector.get_page_url()
        if not current_url:
            return False

        page_keywords = {
            "goods_list": "goods_list",
            "publish": "goods/category",
            "goods_edit": "goods_edit",
        }

        keyword = page_keywords.get(page_type)
        return keyword in current_url if keyword else False

    async def wait_for_page_load(self, page_type: str, timeout: int = 15000) -> bool:
        """
        等待页面加载完成

        Args:
            page_type: 页面类型
            timeout: 超时时间（毫秒）
        """
        start = asyncio.get_event_loop().time()
        timeout_sec = timeout / 1000

        while (asyncio.get_event_loop().time() - start) < timeout_sec:
            if await self.is_on_page(page_type):
                await asyncio.sleep(1)  # 额外等待确保页面稳定
                return True
            await asyncio.sleep(0.5)

        logger.warning(f"等待页面加载超时: {page_type}")
        return False

    async def click_publish_button(self) -> bool:
        """点击发布新商品按钮"""
        logger.info("点击发布新商品按钮")

        # 尝试多种选择器
        selectors = [
            'button:has-text("发布新商品")',
            'button:has-text("发布商品")',
            '.publish-btn',
            '[class*="publish"] button',
        ]

        for selector in selectors:
            try:
                result = await self.connector.evaluate(f"""
                    (() => {{
                        const btn = document.querySelector('{selector}');
                        if (btn) {{
                            btn.click();
                            return true;
                        }}
                        return false;
                    }})()
                """)
                if result:
                    await asyncio.sleep(ACTION_DELAYS["click"])
                    return True
            except Exception:
                continue

        # 使用文本匹配
        result = await self.connector.evaluate("""
            (() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.innerText.includes('发布新商品') || btn.innerText.includes('发布商品')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            })()
        """)

        if result:
            await asyncio.sleep(ACTION_DELAYS["click"])
            return True

        logger.error("未找到发布按钮")
        return False

    async def click_next_step(self) -> bool:
        """点击下一步按钮"""
        logger.info("点击下一步按钮")

        result = await self.connector.evaluate("""
            (() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.innerText.includes('下一步') || btn.innerText.includes('完善商品信息')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            })()
        """)

        if result:
            await asyncio.sleep(ACTION_DELAYS["page_load"])
            return True

        logger.error("未找到下一步按钮")
        return False

    async def click_submit(self) -> bool:
        """点击提交/发布按钮"""
        logger.info("点击提交按钮")

        result = await self.connector.evaluate("""
            (() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.innerText.includes('提交') || btn.innerText.includes('发布')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            })()
        """)

        if result:
            await asyncio.sleep(ACTION_DELAYS["page_load"])
            return True

        logger.error("未找到提交按钮")
        return False

    async def switch_tab(self, tab_name: str) -> bool:
        """
        切换Tab

        Args:
            tab_name: Tab名称 (以图发品, 搜索发品)
        """
        logger.info(f"切换到Tab: {tab_name}")

        result = await self.connector.evaluate(f"""
            (() => {{
                const tabs = document.querySelectorAll('.search-tab-label, [class*="tab"]');
                for (const tab of tabs) {{
                    if (tab.innerText.includes('{tab_name}')) {{
                        tab.click();
                        return true;
                    }}
                }}
                return false;
            }})()
        """)

        if result:
            await asyncio.sleep(ACTION_DELAYS["click"])
            return True

        logger.error(f"未找到Tab: {tab_name}")
        return False

    async def get_current_step(self) -> int:
        """获取当前发布步骤"""
        text = await self.connector.get_page_text()

        if "第1步" in text or "上传" in text:
            return 1
        elif "第2步" in text or "完善" in text:
            return 2
        elif "第3步" in text or "确认" in text:
            return 3

        return 0

    async def take_screenshot(self, name: str = "page") -> str:
        """截取当前页面截图"""
        from config import DATA_DIR
        save_path = str(Path(DATA_DIR["output"]) / f"{name}.png")
        return await self.connector.screenshot(save_path, full_page=True)
