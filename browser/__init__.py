"""
浏览器自动化模块
"""
from .connector import ChromeConnector
from .navigator import PageNavigator

__all__ = ["ChromeConnector", "PageNavigator"]
