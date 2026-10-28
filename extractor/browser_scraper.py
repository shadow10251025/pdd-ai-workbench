"""
浏览器抓取模块
使用Chrome浏览器抓取1688/淘宝商品信息
"""
import asyncio
import re
import json
import os
from typing import Dict, List, Optional
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from loguru import logger

import sys
sys.path.append(str(Path(__file__).parent.parent))
from browser.connector import ChromeConnector
from config import IMAGE_CONFIG, DATA_DIR


class BrowserScraper:
    """浏览器商品抓取器"""

    def __init__(self, connector: ChromeConnector = None):
        self.connector = connector
        self.temp_dir = Path(DATA_DIR["images"]) / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def init(self, connector: ChromeConnector = None):
        """初始化"""
        if connector:
            self.connector = connector
        elif not self.connector:
            self.connector = ChromeConnector()
            await self.connector.connect()

    async def scrape_1688(self, url: str) -> Optional[Dict]:
        """
        抓取1688商品信息

        Args:
            url: 1688商品URL

        Returns:
            商品数据字典
        """
        logger.info(f"抓取1688商品: {url}")

        try:
            # 导航到商品页面
            await self.connector.navigate(url)
            await asyncio.sleep(3)

            # 等待页面加载
            await self.connector.wait_for_element('h1', timeout=10000)

            # 提取商品信息
            product = await self.connector.evaluate("""
                (() => {
                    const result = {
                        title: '',
                        price: 0,
                        original_price: 0,
                        images: [],
                        description: '',
                        attributes: {},
                        specs: []
                    };

                    // 标题
                    const titleEl = document.querySelector('h1, [class*="title"], [class*="Title"]');
                    if (titleEl) result.title = titleEl.innerText.trim();

                    // 价格
                    const priceEls = document.querySelectorAll('[class*="price"], [class*="Price"]');
                    for (const el of priceEls) {
                        const text = el.innerText;
                        const match = text.match(/[\\d.]+/);
                        if (match) {
                            const price = parseFloat(match[0]);
                            if (price > 0 && result.price === 0) {
                                result.price = price;
                            } else if (price > result.price) {
                                result.original_price = price;
                            }
                        }
                    }

                    // 图片
                    const imgEls = document.querySelectorAll('[class*="detail"] img, [class*="gallery"] img, [class*="slide"] img');
                    for (const img of imgEls) {
                        let src = img.src || img.getAttribute('data-src');
                        if (src && src.startsWith('//')) src = 'https:' + src;
                        if (src && !src.includes('favicon') && !src.includes('logo')) {
                            result.images.push(src);
                        }
                    }

                    // 去重图片
                    result.images = [...new Set(result.images)].slice(0, 10);

                    // 属性
                    const attrEls = document.querySelectorAll('[class*="attr"] tr, [class*="param"] tr, [class*="spec"] tr');
                    for (const el of attrEls) {
                        const text = el.innerText;
                        const parts = text.split('\\n');
                        if (parts.length >= 2) {
                            result.attributes[parts[0].trim()] = parts[1].trim();
                        }
                    }

                    return result;
                })()
            """)

            if product:
                product["source_url"] = url
                product["platform"] = "1688"
                logger.info(f"抓取成功: {product.get('title', '未知')[:30]}...")

                # 下载图片
                if product.get("images"):
                    product["local_images"] = await self._download_images(product["images"], "1688")

            return product

        except Exception as e:
            logger.error(f"抓取1688失败: {e}")
            return None

    async def scrape_taobao(self, url: str) -> Optional[Dict]:
        """
        抓取淘宝/天猫商品信息

        Args:
            url: 淘宝/天猫商品URL

        Returns:
            商品数据字典
        """
        logger.info(f"抓取淘宝商品: {url}")

        try:
            # 导航到商品页面
            await self.connector.navigate(url)
            await asyncio.sleep(3)

            # 等待页面加载
            await self.connector.wait_for_element('[class*="Title"], h1', timeout=10000)

            # 提取商品信息
            product = await self.connector.evaluate("""
                (() => {
                    const result = {
                        title: '',
                        price: 0,
                        original_price: 0,
                        images: [],
                        description: '',
                        attributes: {},
                        specs: []
                    };

                    // 标题 - 淘宝/天猫
                    const titleEl = document.querySelector('[class*="Title"] h3, [class*="title"] h3, h3[class*="main"], h1');
                    if (titleEl) result.title = titleEl.innerText.trim();

                    // 价格
                    const priceEl = document.querySelector('[class*="price"] [class*="text"], [class*="Price"] em, .tm-price');
                    if (priceEl) {
                        const match = priceEl.innerText.match(/[\\d.]+/);
                        if (match) result.price = parseFloat(match[0]);
                    }

                    // 图片
                    const imgEls = document.querySelectorAll('[class*="pic"] img, [class*="Pic"] img, [class*="slider"] img, [class*="thumb"] img');
                    for (const img of imgEls) {
                        let src = img.src || img.getAttribute('data-src');
                        if (src && src.startsWith('//')) src = 'https:' + src;
                        if (src && !src.includes('favicon') && !src.includes('logo') && !src.includes('icon')) {
                            result.images.push(src);
                        }
                    }

                    // 去重图片
                    result.images = [...new Set(result.images)].slice(0, 10);

                    // 属性
                    const attrEls = document.querySelectorAll('[class*="attr"] li, [class*=" Attr"] li, [class*="prop"] li');
                    for (const el of attrEls) {
                        const text = el.innerText;
                        const parts = text.split('：');
                        if (parts.length >= 2) {
                            result.attributes[parts[0].trim()] = parts[1].trim();
                        }
                    }

                    return result;
                })()
            """)

            if product:
                product["source_url"] = url
                product["platform"] = "taobao"
                logger.info(f"抓取成功: {product.get('title', '未知')[:30]}...")

                # 下载图片
                if product.get("images"):
                    product["local_images"] = await self._download_images(product["images"], "taobao")

            return product

        except Exception as e:
            logger.error(f"抓取淘宝失败: {e}")
            return None

    async def _download_images(self, image_urls: List[str], platform: str) -> List[str]:
        """
        下载商品图片

        Args:
            image_urls: 图片URL列表
            platform: 平台名称

        Returns:
            本地图片路径列表
        """
        import aiohttp

        save_dir = self.temp_dir / platform
        save_dir.mkdir(exist_ok=True)

        local_paths = []

        async with aiohttp.ClientSession() as session:
            for i, url in enumerate(image_urls[:IMAGE_CONFIG["max_count"]]):
                try:
                    # 处理URL
                    if url.startswith("//"):
                        url = "https:" + url

                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 200:
                            # 确定文件扩展名
                            content_type = response.headers.get("content-type", "")
                            if "png" in content_type:
                                ext = ".png"
                            else:
                                ext = ".jpg"

                            # 保存文件
                            filename = f"image_{i+1:02d}{ext}"
                            filepath = save_dir / filename

                            data = await response.read()
                            with open(filepath, "wb") as f:
                                f.write(data)

                            local_paths.append(str(filepath))
                            logger.info(f"下载图片: {filename}")

                except Exception as e:
                    logger.warning(f"下载图片失败: {url}, {e}")

        return local_paths

    async def scrape_product(self, url: str) -> Optional[Dict]:
        """
        自动识别平台并抓取商品

        Args:
            url: 商品URL

        Returns:
            商品数据字典
        """
        parsed = urlparse(url)
        host = parsed.hostname or ""

        if "1688.com" in host:
            return await self.scrape_1688(url)
        elif "taobao.com" in host or "tmall.com" in host:
            return await self.scrape_taobao(url)
        else:
            logger.error(f"不支持的平台: {host}")
            return None

    async def batch_scrape(self, urls: List[str]) -> List[Dict]:
        """
        批量抓取商品

        Args:
            urls: 商品URL列表

        Returns:
            商品数据列表
        """
        results = []
        for url in urls:
            product = await self.scrape_product(url)
            if product:
                results.append(product)
            else:
                results.append({"url": url, "error": "抓取失败"})
        return results
