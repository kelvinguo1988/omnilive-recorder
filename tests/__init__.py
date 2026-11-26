"""live-recorder 回归测试。

运行方式（仓库根目录）：
    python -m unittest discover -s tests -t . -v

注意要把发现范围限定在 tests/：从根目录直接 discover 会先导入 app 包，
触发 app/config.py 在导入期创建输出目录（容器外可能没有该路径的写权限）。
"""
