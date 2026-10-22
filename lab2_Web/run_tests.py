"""统一运行整合版的 A/B/C 模块测试和端到端回归测试。"""
import argparse
from pathlib import Path
import sys
import unittest


def prepare_workspace():
    root = str(Path(__file__).resolve().parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="默认即运行全部测试")
    args = parser.parse_args()
    prepare_workspace()
    names = ("test_proxy_server", "test_cache", "test_origin_client", "test_integration")
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in names)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
