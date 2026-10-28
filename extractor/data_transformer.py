"""
数据转换器
支持从Excel/CSV导入商品数据
"""
import os
from typing import List, Dict, Optional
from pathlib import Path

import pandas as pd
from loguru import logger

import sys
sys.path.append(str(Path(__file__).parent.parent))
from config import EXCEL_CONFIG


class DataTransformer:
    """数据转换器"""

    def __init__(self):
        self.required_columns = EXCEL_CONFIG["required_columns"]
        self.optional_columns = EXCEL_CONFIG["optional_columns"]

    def import_from_excel(
        self,
        file_path: str,
        sheet_name: int = None
    ) -> List[Dict]:
        """
        从Excel文件导入商品数据

        Args:
            file_path: Excel文件路径
            sheet_name: 工作表名称或索引

        Returns:
            商品数据列表
        """
        if not os.path.exists(file_path):
            logger.error(f"文件不存在: {file_path}")
            return []

        sheet = sheet_name if sheet_name is not None else EXCEL_CONFIG["sheet_name"]

        try:
            # 读取Excel文件
            df = pd.read_excel(file_path, sheet_name=sheet)
            logger.info(f"读取Excel文件: {file_path}, 共 {len(df)} 行")

            # 验证必需列
            missing_cols = [col for col in self.required_columns if col not in df.columns]
            if missing_cols:
                logger.error(f"缺少必需列: {missing_cols}")
                logger.info(f"可用列: {list(df.columns)}")
                return []

            # 转换为商品数据列表
            products = []
            for index, row in df.iterrows():
                product = self._row_to_product(row, index + 1)
                if product:
                    products.append(product)

            logger.info(f"成功导入 {len(products)} 个商品")
            return products

        except Exception as e:
            logger.error(f"导入Excel文件失败: {e}")
            return []

    def import_from_csv(
        self,
        file_path: str,
        encoding: str = "utf-8"
    ) -> List[Dict]:
        """
        从CSV文件导入商品数据

        Args:
            file_path: CSV文件路径
            encoding: 文件编码

        Returns:
            商品数据列表
        """
        if not os.path.exists(file_path):
            logger.error(f"文件不存在: {file_path}")
            return []

        try:
            # 尝试不同编码读取
            for enc in [encoding, "gbk", "gb2312", "latin1"]:
                try:
                    df = pd.read_csv(file_path, encoding=enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                logger.error(f"无法读取CSV文件，编码问题: {file_path}")
                return []

            logger.info(f"读取CSV文件: {file_path}, 共 {len(df)} 行")

            # 验证必需列
            missing_cols = [col for col in self.required_columns if col not in df.columns]
            if missing_cols:
                logger.error(f"缺少必需列: {missing_cols}")
                logger.info(f"可用列: {list(df.columns)}")
                return []

            # 转换为商品数据列表
            products = []
            for index, row in df.iterrows():
                product = self._row_to_product(row, index + 1)
                if product:
                    products.append(product)

            logger.info(f"成功导入 {len(products)} 个商品")
            return products

        except Exception as e:
            logger.error(f"导入CSV文件失败: {e}")
            return []

    def _row_to_product(self, row: pd.Series, row_num: int) -> Optional[Dict]:
        """
        将DataFrame行转换为商品数据

        Args:
            row: DataFrame行
            row_num: 行号

        Returns:
            商品数据字典
        """
        try:
            product = {
                "row_num": row_num,
                "title": str(row.get("商品标题", "")),
                "price": float(row.get("价格", 0)),
                "stock": int(row.get("库存", 100)),
                "category": str(row.get("分类", "")),
                "brand": str(row.get("品牌", "")),
                "material": str(row.get("材质", "")),
                "style": str(row.get("风格", "")),
                "description": str(row.get("商品描述", "")),
                "images": self._parse_images(row.get("图片路径", "")),
                "attributes": {}
            }

            # 构建属性字典
            for col in self.optional_columns:
                if col not in ["商品标题", "价格", "库存", "分类", "图片路径", "商品描述"]:
                    value = row.get(col)
                    if pd.notna(value) and str(value).strip():
                        product["attributes"][col] = str(value)

            # 验证数据
            if not product["title"]:
                logger.warning(f"第{row_num}行: 商品标题为空")
                return None

            if product["price"] <= 0:
                logger.warning(f"第{row_num}行: 价格无效")
                return None

            return product

        except Exception as e:
            logger.error(f"第{row_num}行数据转换失败: {e}")
            return None

    def _parse_images(self, images_str: str) -> List[str]:
        """
        解析图片路径字符串

        Args:
            images_str: 图片路径字符串（支持逗号、分号、换行分隔）

        Returns:
            图片路径列表
        """
        if not images_str or pd.isna(images_str):
            return []

        # 支持多种分隔符
        separators = [",", ";", "\n", "|"]
        images = [images_str]
        for sep in separators:
            if sep in images_str:
                images = images_str.split(sep)
                break

        # 清理路径
        cleaned = []
        for img in images:
            img = img.strip()
            if img and os.path.exists(img):
                cleaned.append(img)
            elif img:
                logger.warning(f"图片文件不存在: {img}")

        return cleaned

    def export_to_excel(
        self,
        products: List[Dict],
        output_path: str
    ) -> bool:
        """
        导出商品数据到Excel

        Args:
            products: 商品数据列表
            output_path: 输出文件路径

        Returns:
            是否成功
        """
        try:
            df = pd.DataFrame(products)
            df.to_excel(output_path, index=False, engine="openpyxl")
            logger.info(f"已导出 {len(products)} 个商品到: {output_path}")
            return True
        except Exception as e:
            logger.error(f"导出Excel失败: {e}")
            return False

    def validate_data(self, products: List[Dict]) -> Dict:
        """
        验证商品数据

        Args:
            products: 商品数据列表

        Returns:
            验证结果
        """
        valid = []
        invalid = []

        for product in products:
            errors = []

            if not product.get("title"):
                errors.append("缺少商品标题")

            if not product.get("price") or product["price"] <= 0:
                errors.append("价格无效")

            if product.get("images") is not None and len(product["images"]) == 0:
                errors.append("没有有效的图片")

            if errors:
                invalid.append({"product": product, "errors": errors})
            else:
                valid.append(product)

        return {
            "valid_count": len(valid),
            "invalid_count": len(invalid),
            "valid": valid,
            "invalid": invalid
        }

    def get_template(self) -> pd.DataFrame:
        """
        获取Excel导入模板

        Returns:
            模板DataFrame
        """
        columns = self.required_columns + self.optional_columns
        template = pd.DataFrame(columns=columns)

        # 添加示例数据
        example = {
            "商品标题": "示例商品标题",
            "价格": 99.9,
            "库存": 100,
            "分类": "服装",
            "品牌": "示例品牌",
            "材质": "棉",
            "风格": "休闲",
            "图片路径": "/path/to/image1.jpg,/path/to/image2.jpg",
            "商品描述": "商品描述内容"
        }

        template = pd.DataFrame([example])
        return template

    def save_template(self, output_path: str) -> bool:
        """
        保存Excel导入模板

        Args:
            output_path: 输出路径

        Returns:
            是否成功
        """
        try:
            template = self.get_template()
            template.to_excel(output_path, index=False, engine="openpyxl")
            logger.info(f"模板已保存: {output_path}")
            return True
        except Exception as e:
            logger.error(f"保存模板失败: {e}")
            return False
