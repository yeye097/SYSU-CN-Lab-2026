"""执行 python run_tests.py，运行 A 与 C 的全部测试，失败时返回非零退出码。"""
import sys
import unittest
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    # C 原测试从自身目录导入测试源站，追加路径即可，无需修改原测试。
    sys.path.insert(0, str(root))
    sys.path.append(str(root / "C"))
    suite = unittest.TestSuite()
    for name in ("test_proxy_server", "test_origin_client"):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
