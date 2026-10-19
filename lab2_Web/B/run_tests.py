"""B 测试入口：准备 A/B/C 导入路径，--all 同时运行 A/C 的原有测试。"""
import argparse
import sys
import unittest
from pathlib import Path


def prepare_workspace():
    """兼容分目录交付和最终根目录集成，不复制公共类型或异常类。"""
    here = Path(__file__).resolve().parent
    if here.name == "B" and (here.parent / "A").is_dir():
        # A 的公共入口按扁平模块导入，再由入口引用 C 子目录中的真实类。
        paths = (here, here.parent / "A", here.parent, here.parent / "C")
    else:
        paths = (here, here / "C")
    # 从后往前插入，保证最终搜索优先级与 paths 的顺序一致。
    for path in reversed(paths):
        value = str(path)
        if value in sys.path:
            sys.path.remove(value)
        sys.path.insert(0, value)


def main():
    parser = argparse.ArgumentParser(description="运行 B 缓存模块测试")
    parser.add_argument("--all", action="store_true", help="同时运行 A 和 C 原有测试")
    args = parser.parse_args()
    prepare_workspace()
    names = ["test_cache"]
    if args.all:
        names.extend(["test_proxy_server", "test_origin_client"])
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in names)
    # 返回非零退出码，让终端和自动化工具能够识别测试失败。
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
