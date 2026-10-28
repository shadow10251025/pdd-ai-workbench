"""
图片处理工具
"""
import os
from pathlib import Path
from typing import List, Tuple, Optional

from PIL import Image
from loguru import logger

import sys
sys.path.append(str(Path(__file__).parent.parent))
from config import IMAGE_CONFIG


class ImageProcessor:
    """图片处理器"""

    def __init__(self):
        self.max_count = IMAGE_CONFIG["max_count"]
        self.min_width = IMAGE_CONFIG["min_width"]
        self.min_height = IMAGE_CONFIG["min_height"]
        self.max_size_mb = IMAGE_CONFIG["max_size_mb"]
        self.allowed_formats = IMAGE_CONFIG["allowed_formats"]

    def validate_image(self, image_path: str) -> Tuple[bool, str]:
        """
        验证图片是否符合要求

        Args:
            image_path: 图片路径

        Returns:
            (是否有效, 错误信息)
        """
        path = Path(image_path)

        # 检查文件是否存在
        if not path.exists():
            return False, f"文件不存在: {image_path}"

        # 检查文件格式
        suffix = path.suffix.lower().replace(".", "")
        if suffix not in self.allowed_formats:
            return False, f"不支持的格式: {suffix}，支持: {self.allowed_formats}"

        # 检查文件大小
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > self.max_size_mb:
            return False, f"文件过大: {size_mb:.1f}MB，最大: {self.max_size_mb}MB"

        # 检查图片尺寸
        try:
            with Image.open(image_path) as img:
                width, height = img.size
                if width < self.min_width or height < self.min_height:
                    return False, f"图片尺寸过小: {width}x{height}，最小: {self.min_width}x{self.min_height}"
        except Exception as e:
            return False, f"无法读取图片: {e}"

        return True, ""

    def process_image(
        self,
        image_path: str,
        output_path: str = None,
        target_ratio: str = None,
        max_size_mb: float = None
    ) -> str:
        """
        处理图片（裁剪、压缩）

        Args:
            image_path: 原始图片路径
            output_path: 输出路径
            target_ratio: 目标宽高比 (1:1, 3:4)
            max_size_mb: 最大文件大小

        Returns:
            处理后的图片路径
        """
        if not output_path:
            output_path = str(Path(image_path).parent / f"processed_{Path(image_path).name}")

        max_size_mb = max_size_mb or self.max_size_mb

        try:
            with Image.open(image_path) as img:
                # 转换为RGB（如果是RGBA）
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                # 裁剪到目标宽高比
                if target_ratio:
                    img = self._crop_to_ratio(img, target_ratio)

                # 压缩到目标大小
                img = self._compress_to_size(img, max_size_mb)

                # 保存
                img.save(output_path, "JPEG", quality=95)
                logger.info(f"图片已处理: {output_path}")

                return output_path

        except Exception as e:
            logger.error(f"处理图片失败: {e}")
            return image_path

    def _crop_to_ratio(self, img: Image.Image, ratio: str) -> Image.Image:
        """
        裁剪图片到指定宽高比

        Args:
            img: PIL图片对象
            ratio: 目标宽高比 (1:1, 3:4)
        """
        width, height = img.size
        target_w, target_h = map(int, ratio.split(":"))

        # 计算目标尺寸
        current_ratio = width / height
        target_ratio = target_w / target_h

        if current_ratio > target_ratio:
            # 图片太宽，裁剪宽度
            new_width = int(height * target_ratio)
            left = (width - new_width) // 2
            img = img.crop((left, 0, left + new_width, height))
        elif current_ratio < target_ratio:
            # 图片太高，裁剪高度
            new_height = int(width / target_ratio)
            top = (height - new_height) // 2
            img = img.crop((0, top, width, top + new_height))

        return img

    def _compress_to_size(self, img: Image.Image, max_size_mb: float) -> Image.Image:
        """
        压缩图片到指定大小

        Args:
            img: PIL图片对象
            max_size_mb: 最大文件大小(MB)
        """
        # 如果图片已经够小，直接返回
        if img.size[0] * img.size[1] < 1000000:  # 小于100万像素
            return img

        # 缩放图片
        max_pixels = 2000 * 2000  # 最大2000x2000
        current_pixels = img.size[0] * img.size[1]

        if current_pixels > max_pixels:
            ratio = (max_pixels / current_pixels) ** 0.5
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        return img

    def batch_process(
        self,
        image_paths: List[str],
        output_dir: str = None,
        target_ratio: str = "1:1"
    ) -> List[str]:
        """
        批量处理图片

        Args:
            image_paths: 图片路径列表
            output_dir: 输出目录
            target_ratio: 目标宽高比

        Returns:
            处理后的图片路径列表
        """
        if not output_dir:
            output_dir = str(Path(image_paths[0]).parent / "processed")

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        processed = []
        for i, path in enumerate(image_paths[:self.max_count]):
            # 验证图片
            valid, error = self.validate_image(path)
            if not valid:
                logger.warning(f"跳过图片 {path}: {error}")
                continue

            # 处理图片
            output_path = str(Path(output_dir) / f"image_{i+1:02d}.jpg")
            result = self.process_image(path, output_path, target_ratio)
            processed.append(result)

        logger.info(f"批量处理完成: {len(processed)}/{len(image_paths)} 张图片")
        return processed

    def get_image_info(self, image_path: str) -> dict:
        """
        获取图片信息

        Args:
            image_path: 图片路径

        Returns:
            图片信息字典
        """
        try:
            with Image.open(image_path) as img:
                return {
                    "path": image_path,
                    "format": img.format,
                    "mode": img.mode,
                    "size": img.size,
                    "width": img.size[0],
                    "height": img.size[1],
                    "file_size": Path(image_path).stat().st_size,
                }
        except Exception as e:
            logger.error(f"获取图片信息失败: {e}")
            return {}
