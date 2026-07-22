"""flawless 后端兼容入口。

生产启动命令继续使用 ``backend.app.main:app``，实际应用装配位于
``backend.app.application``。这样部署契约不变，同时避免入口文件再次膨胀。
"""

from __future__ import annotations

import sys

from backend.app import application as _application


# 将兼容模块直接指向真实应用模块。历史测试或扩展代码中对私有函数的
# monkeypatch 仍会作用于同一模块对象，迁移期间不会出现“双份全局状态”。
sys.modules[__name__] = _application
