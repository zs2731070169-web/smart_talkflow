"""conf.config 全局配置加载测试。

迁移自 ``src/conf/config.py`` 的 ``__main__`` 段:验证全局配置单例能成功
加载(模块级单例 ``settings`` 导入即完成必填校验),并保证拼好的 MySQL
连接串格式正确。

运行::

    python -m unittest tests.test_config
"""
import unittest

from conf.config import settings


class ConfigTest(unittest.TestCase):
    """全局配置(settings)加载校验。"""

    def test_settings_loaded(self):
        """能走到断言即说明 ``settings`` 实例化(必填校验)通过。"""
        # 与原 __main__ 一致:遍历打印所有配置字段,便于人工核对
        for key, value in settings.__dict__.items():
            print(key, value)

        # 必填项应非空
        self.assertTrue(settings.mysql_host)
        self.assertTrue(settings.mysql_database)
        self.assertTrue(settings.mysql_user)
        self.assertTrue(settings.mysql_password)
        # 拼好的连接串应以 asyncmy 驱动头开头
        self.assertTrue(settings.mysql_conf.startswith("mysql+asyncmy://"))


if __name__ == "__main__":
    unittest.main()
