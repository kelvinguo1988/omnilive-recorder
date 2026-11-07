"""构建期依赖校验：确保 requirements.txt 中的直接依赖均已安装到当前环境。

背景：项目用 pip-compile 生成的 requirements.lock 锁定传递依赖，但 lock 可能与
requirements.txt 不同步（曾出现 lock 缺少 gmssl，构建阶段不报错、容器启动才
ModuleNotFoundError）。本脚本在镜像构建阶段显式校验直接依赖已安装，
把「运行时崩溃」提前为「构建失败」。

校验视角与 pip 一致（distribution 是否安装，importlib.metadata），
而非「模块名能否 import」——PyPI 包名与 import 模块名经常不一致
（PyYAML→yaml、Pillow→PIL、beautifulsoup4→bs4…），按模块名判定会把
已正确安装的包误报为缺失、打挂构建。

用法：
    python scripts/check_deps.py [requirements文件路径]
默认读取项目根目录下的 requirements.txt。
"""
import importlib.metadata
import pathlib
import sys


def parse_requirement(line: str) -> str:
    """从一行 requirement 中解析 PyPI 包名（剥离版本约束/extras/直接引用/环境标记）。"""
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("-"):
        return ""
    # 去掉行内注释
    line = line.split(" #", 1)[0].strip()
    # 依次剥离版本约束/直接引用(@)/环境标记(;)。
    # "===" 必须在 "==" 之前，否则永远先被 "==" 截断（死条目）；
    # ">=" / "<=" 必须在 ">" / "<" 之前，避免截断到一半留下残字符。
    for sep in ("===", "==", ">=", "<=", "~=", "!=", ">", "<", "@", ";"):
        if sep in line:
            line = line.split(sep, 1)[0]
    # 剥离 extras，如 uvicorn[standard]
    if "[" in line:
        line = line.split("[", 1)[0]
    return line.strip()


def distribution_installed(package: str) -> bool:
    """该 distribution 是否已安装（importlib.metadata 按 PEP 503 规范化匹配，
    大小写/`-`/`_`/`.` 差异均不影响判定）。"""
    try:
        importlib.metadata.distribution(package)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def main(argv: list) -> int:
    req_path = pathlib.Path(argv[1]) if len(argv) > 1 else \
        pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"

    if not req_path.exists():
        print(f"[deps] 未找到依赖文件: {req_path}", file=sys.stderr)
        return 1

    missing = []
    checked = 0
    for raw in req_path.read_text(encoding="utf-8").splitlines():
        pkg = parse_requirement(raw)
        if not pkg:
            continue
        checked += 1
        if not distribution_installed(pkg):
            missing.append(pkg)

    if missing:
        print("[deps] 依赖校验失败，以下包未安装到当前环境：", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print(f"[deps] 已检查 {checked} 个直接依赖。请确认 requirements.lock "
              f"与 requirements.txt 已同步（必要时重新 pip-compile）。", file=sys.stderr)
        return 1

    print(f"[deps] 依赖校验通过：{checked} 个直接依赖均已安装")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
