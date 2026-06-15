"""测试包初始化。

在测试模块导入前把 ``src`` 加入 ``sys.path``,使测试代码能够以
``conf`` / ``infra`` / ``engine`` / ``utils`` 等顶层包的方式导入源码,
与源码内部 ``from conf.config import settings`` 的导入口径保持一致。

运行(在项目根目录执行)::

    python -m unittest discover -s tests      # 运行全部测试
    python -m unittest tests.test_database    # 运行单个测试模块
"""
import sys
from pathlib import Path

# 项目根下的 src 目录即包根
_SRC_PATH = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(_SRC_PATH))
