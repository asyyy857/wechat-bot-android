# -*- coding: utf-8 -*-
"""wechat-auto：微信（PC 4.x）人设自动回复机器人。

分层
----
reader   读取层（截屏 + OCR）—— 平台相关，手机端换这里
matcher  匹配层（关键词模板）—— 0 Token
brain    判断层（本地打分 + 大模型）
governor 节流层（频率 / 静默 / 连发限制）
sender   发送层 —— 平台相关，手机端换这里
bot      编排层
"""
from .bot import Bot, setup_logger
from .brain import Brain
from .config import load_config, load_persona, load_rules
from .drivers import Reader, Sender
from .governor import Governor
from .matcher import Matcher, Rule
from .reader import Block, Layout, Message, PcReader, Snapshot, normalize, tidy
from .sender import PcSender
from .state import State

__all__ = [
    "Bot", "setup_logger", "Brain", "Governor", "Matcher", "Rule", "State",
    "PcReader", "PcSender", "Reader", "Sender", "Block", "Layout", "Message",
    "Snapshot", "normalize", "tidy", "load_config", "load_rules", "load_persona",
]
__version__ = "0.2.0"
