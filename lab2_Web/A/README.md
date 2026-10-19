# 成员 A 交付文件

此文件夹仅含 A 新增文件，不含 B、C 实现。

使用时将本文件夹中的文件复制到原仓库的 `lab2_Web` 目录，与现有 `C` 文件夹同级；不要直接在独立的 A 文件夹中启动。
`origin_client.py` 和 `http_types.py` 是 A 编写的接入文件，引用现有 C 实现，并非 C 源代码副本。

B 未完成时：`python proxy_server.py --port 8888 --no-cache`。
A 模块测试：`python -m unittest test_proxy_server -v`。
A+C 全部测试：`python run_tests.py`。
具体接口与运行顺序见 `A_实现与联调.md`。
