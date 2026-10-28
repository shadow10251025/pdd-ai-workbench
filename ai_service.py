"""
AI 服务降级桩

原项目依赖的 ai_service 模块缺失，这里提供一个最小实现，
让 /api/ai/optimize-title 和 /api/ai/generate-desc 端点不再抛 ImportError，
返回原样/简单模板结果。核心的抓取与上架流程不依赖 AI，完全不受影响。
"""


class AIService:
    """降级版 AI 服务：不调用任何外部 API，返回降级结果。"""

    async def optimize_title(self, title: str) -> str:
        """标题优化：降级为原样返回。"""
        return title

    async def generate_description(self, product_info: dict) -> str:
        """描述生成：降级为简单模板。"""
        name = product_info.get("name") or product_info.get("title") or "本商品"
        return f"{name}，品质优良，欢迎选购。详情请见页面图文描述。"
