"""构建期依赖校验：确保 requirements.txt 中的直接依赖均已安装到当前环境。

背景：项目用 pip-compile 生成的 requirements.lock 锁定传递依赖，但 lock 可能与
requirements.txt 不同步（曾出现 lock 缺少 gmssl，构建阶段不报错、容器启动才
ModuleNotFoundError）。本脚本在镜像构建阶段显式校验直接依赖的可导入性，
把「运行时崩溃」提前为「构建失败」。

用法：
    python scripts/check_deps.py [requirements文件路径]
默认读取项目根目录下的 requirements.txt。
"""
import importlib.util
import pathlib
import sys

# 部分 PyPI 包名与 import 模块名不一致，需显式映射。
PACKAGE_TO_MODULE = {
    "python-multipart": "multipart",
    "python-dotenv": "dotenv",
    "pydantic-settings": "pydantic_settings",
    "uvicorn": "uvicorn",
    "websockets": "websockets",
}


def parse_requirement(line: str) -> str:
    """从一行 requirement 中解析包名（剥离版本约束与 extras）。"""
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("-"):
        return ""
    # 去掉行内注释
    line = line.split(" #", 1)[0].strip()
    # 依次剥离各种版本约束符
    for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "==="):
        if sep in line:
            line = line.split(sep, 1)[0]
    # 剥离 extras，如 uvicorn[standard]
    if "[" in line:
        line = line.split("[", 1)[0]
    return line.strip()


def module_name_for(package: str) -> str:
    if package in PACKAGE_TO_MODULE:
        return PACKAGE_TO_MODULE[package]
    return package.replace("-", "_")


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
        mod = module_name_for(pkg)
        checked += 1
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(f"{pkg} (import {mod})")
        except (ImportError, ValueError):
            missing.append(f"{pkg} (import {mod})")

    if missing:
        print("[deps] 依赖校验失败，以下包未安装到当前环境：", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print(f"[deps] 已检查 {checked} 个直接依赖。请确认 requirements.lock "
              f"与 requirements.txt 已同步（必要时重新 pip-compile）。", file=sys.stderr)
        return 1

    print(f"[deps] 依赖校验通过：{checked} 个直接依赖均可导入")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
