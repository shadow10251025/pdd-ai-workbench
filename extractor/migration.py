"""
1688/淘宝商品搬家模块
从其他电商平台抓取商品信息并转换为拼多多格式
"""
import asyncio
from typing import Dict, List, Optional
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from loguru import logger

import sys
sys.path.append(str(Path(__file__).parent.parent))
from browser.connector import ChromeConnector
from extractor.browser_scraper import BrowserScraper
from config import MIGRATION_CONFIG, DATA_DIR


class ProductMigration:
    """商品搬家器"""

    def __init__(self):
        self.supported_platforms = MIGRATION_CONFIG["supported_platforms"]
        self.price_margin = MIGRATION_CONFIG["price_adjustment"]["default_margin"]
        self.scraper = None

    async def init(self, connector: ChromeConnector = None):
        """初始化"""
        self.scraper = BrowserScraper(connector)
        await self.scraper.init(connector)

    def extract_url_info(self, url: str) -> Dict:
        """
        从URL中提取平台和商品ID

        Args:
            url: 商品URL

        Returns:
            平台信息字典
        """
        import re
        parsed = urlparse(url)
        host = parsed.hostname or ""

        info = {
            "url": url,
            "platform": None,
            "product_id": None,
        }

        # 1688
        if "1688.com" in host:
            info["platform"] = "1688"
            if "/offer/" in parsed.path:
                match = re.search(r'/offer/(\d+)', parsed.path)
                if match:
                    info["product_id"] = match.group(1)

        # 淘宝/天猫
        elif "taobao.com" in host or "tmall.com" in host:
            info["platform"] = "taobao"
            if "id=" in parsed.query:
                info["product_id"] = parse_qs(parsed.query).get("id", [None])[0]

        return info

    async def migrate_from_url(self, url: str, connector: ChromeConnector = None) -> Dict:
        """
        从URL迁移商品

        Args:
            url: 商品URL
            connector: Chrome连接器

        Returns:
            商品数据字典
        """
        url_info = self.extract_url_info(url)

        if not url_info["platform"]:
            return {"success": False, "message": "不支持的平台或无效URL"}

        if url_info["platform"] not in self.supported_platforms:
            return {"success": False, "message": f"不支持的平台: {url_info['platform']}"}

        logger.info(f"从{url_info['platform']}迁移商品: {url}")

        try:
            # 初始化scraper
            if not self.scraper:
                await self.init(connector)

            # 抓取商品
            product_data = await self.scraper.scrape_product(url)

            if product_data:
                # 转换为拼多多格式
                pdd_product = self._convert_to_pdd_format(product_data, url_info["platform"])
                return {"success": True, "product": pdd_product}
            else:
                return {"success": False, "message": "抓取商品信息失败"}

        except Exception as e:
            logger.error(f"迁移失败: {e}")
            return {"success": False, "message": str(e)}

    def _convert_to_pdd_format(self, product_data: Dict, source_platform: str) -> Dict:
        """
        将商品数据转换为拼多多格式

        Args:
            product_data: 原始商品数据
            source_platform: 来源平台

        Returns:
            拼多多格式的商品数据
        """
        # 计算拼多多价格（加价）
        original_price = product_data.get("price", 0)
        pdd_price = original_price * self.price_margin

        pdd_product = {
            "title": self._optimize_title(product_data.get("title", "")),
            "price": round(pdd_price, 2),
            "original_price": original_price,
            "stock": product_data.get("stock", 100),
            "images": product_data.get("images", []),
            "local_images": product_data.get("local_images", []),
            "description": product_data.get("description", ""),
            "attributes": product_data.get("attributes", {}),
            "source_url": product_data.get("source_url", ""),
            "source_platform": source_platform,
            "category": product_data.get("category", ""),
            "brand": product_data.get("brand", ""),
        }

        return pdd_product

    def _optimize_title(self, title: str) -> str:
        """
        优化商品标题

        Args:
            title: 原始标题

        Returns:
            优化后的标题
        """
        import re
        # 移除特殊字符
        title = re.sub(r'[【】\[\]{}]', '', title)

        # 移除多余空格
        title = re.sub(r'\s+', ' ', title).strip()

        # 限制长度
        if len(title) > 60:
            title = title[:60]

        return title

    def adjust_price(
        self,
        original_price: float,
        margin: float = None,
        min_price: float = None,
        max_price: float = None
    ) -> float:
        """
        调整价格

        Args:
            original_price: 原始价格
            margin: 加价比例
            min_price: 最低价格
            max_price: 最高价格

        Returns:
            调整后的价格
        """
        margin = margin or self.price_margin
        new_price = original_price * margin

        if min_price and new_price < min_price:
            new_price = min_price
        if max_price and new_price > max_price:
            new_price = max_price

        return round(new_price, 2)
