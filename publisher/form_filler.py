"""
表单自动填写模块
"""
import asyncio
from typing import Dict, Optional, Any
from pathlib import Path

from loguru import logger

import sys
sys.path.append(str(Path(__file__).parent.parent))
from browser.connector import ChromeConnector
from config import ACTION_DELAYS


class FormFiller:
    """表单自动填写器"""

    def __init__(self, connector: ChromeConnector):
        self.connector = connector

    async def fill_form(self, data: Dict) -> bool:
        """
        填写商品表单

        Args:
            data: 表单数据字典，包含：
                - title: 商品标题
                - price: 价格
                - stock: 库存
                - description: 商品描述
                - attributes: 商品属性字典

        Returns:
            是否成功
        """
        try:
            success = True

            # 填写标题
            if data.get("title"):
                if not await self.fill_title(data["title"]):
                    success = False

            # 填写价格
            if data.get("price") is not None:
                if not await self.fill_price(data["price"]):
                    success = False

            # 填写库存
            if data.get("stock") is not None:
                if not await self.fill_stock(data["stock"]):
                    success = False

            # 填写属性
            if data.get("attributes"):
                for attr_name, attr_value in data["attributes"].items():
                    if not await self.fill_attribute(attr_name, attr_value):
                        logger.warning(f"填写属性失败: {attr_name}")

            # 填写描述
            if data.get("description"):
                if not await self.fill_description(data["description"]):
                    success = False

            return success

        except Exception as e:
            logger.error(f"填写表单失败: {e}")
            return False

    async def fill_title(self, title: str) -> bool:
        """
        填写商品标题

        Args:
            title: 商品标题

        Returns:
            是否成功
        """
        return await self._fill_input_by_placeholder("商品标题", title) or \
               await self._fill_input_by_name("goodsName", title) or \
               await self._fill_input_by_class("title", title)

    async def fill_price(self, price: float) -> bool:
        """
        填写价格

        Args:
            price: 价格

        Returns:
            是否成功
        """
        price_str = f"{price:.2f}"
        return await self._fill_input_by_placeholder("价格", price_str) or \
               await self._fill_input_by_placeholder("¥", price_str) or \
               await self._fill_input_by_name("price", price_str)

    async def fill_stock(self, stock: int) -> bool:
        """
        填写库存

        Args:
            stock: 库存数量

        Returns:
            是否成功
        """
        stock_str = str(stock)
        return await self._fill_input_by_placeholder("库存", stock_str) or \
               await self._fill_input_by_placeholder("数量", stock_str) or \
               await self._fill_input_by_name("stock", stock_str)

    async def fill_attribute(self, name: str, value: str) -> bool:
        """
        填写商品属性

        Args:
            name: 属性名称
            value: 属性值

        Returns:
            是否成功
        """
        # 尝试多种方式查找属性输入框
        result = await self.connector.evaluate(f"""
            (() => {{
                // 查找标签
                const labels = document.querySelectorAll('label, [class*="label"]');
                for (const label of labels) {{
                    if (label.innerText.includes('{name}')) {{
                        // 查找对应的输入框
                        const input = label.nextElementSibling?.querySelector('input, select, textarea') ||
                                      label.parentElement?.querySelector('input, select, textarea');
                        if (input) {{
                            input.focus();
                            input.value = '{value}';
                            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return true;
                        }}
                    }}
                }}

                // 尝试通过name属性查找
                const inputs = document.querySelectorAll('input[name*="{name}"], select[name*="{name}"]');
                for (const input of inputs) {{
                    input.focus();
                    input.value = '{value}';
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }}

                return false;
            }})()
        """, )

        return result

    async def fill_description(self, description: str) -> bool:
        """
        填写商品描述

        Args:
            description: 商品描述

        Returns:
            是否成功
        """
        # 尝试查找富文本编辑器或textarea
        result = await self.connector.evaluate(f"""
            (() => {{
                // 查找textarea
                const textareas = document.querySelectorAll('textarea');
                for (const textarea of textareas) {{
                    if (textarea.placeholder?.includes('描述') ||
                        textarea.placeholder?.includes('详情') ||
                        textarea.name?.includes('desc')) {{
                        textarea.focus();
                        textarea.value = `{description}`;
                        textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        return true;
                    }}
                }}

                // 查找富文本编辑器
                const editors = document.querySelectorAll('[contenteditable="true"], [class*="editor"], [class*="Editor"]');
                for (const editor of editors) {{
                    editor.focus();
                    editor.innerHTML = `{description}`;
                    editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    return true;
                }}

                return false;
            }})()
        """)

        return result

    async def _fill_input_by_placeholder(self, placeholder: str, value: str) -> bool:
        """
        通过placeholder查找并填写输入框

        Args:
            placeholder: placeholder文本
            value: 要填写的值

        Returns:
            是否成功
        """
        result = await self.connector.evaluate(f"""
            (() => {{
                const inputs = document.querySelectorAll('input, textarea');
                for (const input of inputs) {{
                    if (input.placeholder?.includes('{placeholder}')) {{
                        input.focus();
                        input.value = '';
                        // 模拟逐字输入
                        const text = '{value}';
                        for (let i = 0; i < text.length; i++) {{
                            const char = text[i];
                            input.value += char;
                            input.dispatchEvent(new KeyboardEvent('keydown', {{ key: char }}));
                            input.dispatchEvent(new KeyboardEvent('keypress', {{ key: char }}));
                            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            input.dispatchEvent(new KeyboardEvent('keyup', {{ key: char }}));
                        }}
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return true;
                    }}
                }}
                return false;
            }})()
        """)

        if result:
            await asyncio.sleep(ACTION_DELAYS["form_fill"])

        return result

    async def _fill_input_by_name(self, name: str, value: str) -> bool:
        """
        通过name属性查找并填写输入框

        Args:
            name: input的name属性
            value: 要填写的值

        Returns:
            是否成功
        """
        result = await self.connector.evaluate(f"""
            (() => {{
                const inputs = document.querySelectorAll('input[name="{name}"], textarea[name="{name}"]');
                for (const input of inputs) {{
                    input.focus();
                    input.value = '{value}';
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }}
                return false;
            }})()
        """)

        if result:
            await asyncio.sleep(ACTION_DELAYS["form_fill"])

        return result

    async def _fill_input_by_class(self, class_name: str, value: str) -> bool:
        """
        通过class名称查找并填写输入框

        Args:
            class_name: class名称的一部分
            value: 要填写的值

        Returns:
            是否成功
        """
        result = await self.connector.evaluate(f"""
            (() => {{
                const inputs = document.querySelectorAll('input[class*="{class_name}"], textarea[class*="{class_name}"]');
                for (const input of inputs) {{
                    input.focus();
                    input.value = '{value}';
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }}
                return false;
            }})()
        """)

        if result:
            await asyncio.sleep(ACTION_DELAYS["form_fill"])

        return result

    async def get_form_fields(self) -> Dict:
        """
        获取当前表单的所有字段信息

        Returns:
            表单字段字典
        """
        result = await self.connector.evaluate("""
            (() => {
                const fields = {};
                const inputs = document.querySelectorAll('input:not([type="hidden"]), textarea, select');
                inputs.forEach((input, index) => {
                    const key = input.name || input.id || `field_${index}`;
                    fields[key] = {
                        type: input.type || input.tagName.toLowerCase(),
                        name: input.name,
                        id: input.id,
                        placeholder: input.placeholder,
                        value: input.value,
                        required: input.required,
                        className: input.className?.substring(0, 100)
                    };
                });
                return fields;
            })()
        """)

        return result or {}

    async def validate_form(self) -> Dict:
        """
        验证表单填写是否完整

        Returns:
            验证结果字典
        """
        result = await self.connector.evaluate("""
            (() => {
                const requiredFields = document.querySelectorAll('[required], [aria-required="true"]');
                const missing = [];
                const filled = [];

                requiredFields.forEach(field => {
                    const value = field.value || field.innerText;
                    if (!value || value.trim() === '') {
                        missing.push({
                            name: field.name || field.id || field.placeholder,
                            type: field.type || field.tagName
                        });
                    } else {
                        filled.push(field.name || field.id);
                    }
                });

                return {
                    valid: missing.length === 0,
                    missing: missing,
                    filled: filled
                };
            })()
        """)

        return result or {"valid": False, "missing": [], "filled": []}

    async def clear_form(self) -> bool:
        """清空表单"""
        result = await self.connector.evaluate("""
            (() => {
                const inputs = document.querySelectorAll('input:not([type="hidden"]), textarea');
                inputs.forEach(input => {
                    input.value = '';
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                });
                return true;
            })()
        """)

        return result
