#!/usr/bin/env python3
"""从 pyproject.toml 的 Poetry 依赖表生成 pip 依赖规格列表，供 uv 使用。

本机没有 Poetry（且构建原产物所用的 CPython 3.11.9 已不存在），因此本地开发环境
改用 uv 管理。为了避免依赖清单出现两份（维护时必然漂移），这里直接以
``[tool.poetry.dependencies]`` 为唯一数据源，转换后交给 uv。

用法::

    python tools/uv_deps.py            # 每行一个依赖规格
    python tools/uv_deps.py --exclude slimeface

依赖 tomllib（Python 3.11+ 标准库）；3.10 下退回 tomli。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

PROJ_ROOT = Path(__file__).resolve().parent.parent

# 标记名按 PEP 508 规范写出，避免和 Poetry 的写法混淆
WINDOWS_MARKER = "sys_platform == 'win32'"


def _is_windows_only(decl) -> bool:
    """判断某个依赖声明是否仅适用于 Windows"""
    if not isinstance(decl, dict):
        return False
    markers = decl.get("markers", "")
    # Poetry 允许 markers 写成列表
    if isinstance(markers, (list, tuple)):
        markers = " ".join(markers)
    return "win32" in markers


def _to_spec(name: str, decl) -> str | None:
    """把 Poetry 的依赖声明转换成 pip 可接受的规格字符串

    Poetry 允许的形式：
        pkg = "1.2.3"                      -> pkg==1.2.3
        pkg = "^1.2"                       -> pkg>=1.2,<2.0.0
        pkg = {version = "^1.2", extras = ["x"]}
    """
    extras = ""
    if isinstance(decl, str):
        constraint = decl
    elif isinstance(decl, dict):
        constraint = decl.get("version", "")
        extra_list = decl.get("extras") or []
        if extra_list:
            extras = f"[{','.join(extra_list)}]"
    else:
        # 表格、路径、git 等复杂来源：原样交给 uv 处理，这里跳过并提示
        print(f"# 跳过无法转换的依赖: {name}", file=sys.stderr)
        return None

    constraint = (constraint or "").strip()
    if not constraint or constraint == "*":
        return f"{name}{extras}"

    # 在唯一一处把 Poetry 的插入符语义翻成 pip 区间语义
    if constraint.startswith("^"):
        base = constraint[1:]
        parts = base.split(".")
        if len(parts) == 1:
            upper = f"{int(parts[0]) + 1}.0.0"
        else:
            major, minor = parts[0], parts[1]
            upper = f"{int(major) + 1}.0.0" if int(major) > 0 else f"0.{int(minor) + 1}.0"
        return f"{name}{extras}>={base},<{upper}"

    # Poetry 的裸版本号是精确锁定语义（如 "1.2.71"），对应 pip 的 "==1.2.71"；
    # 若已带比较运算符（>=、~=、!= 等）则原样透传
    if constraint[0].isdigit():
        return f"{name}{extras}=={constraint}"

    return f"{name}{extras}{constraint}"


def build_specs(exclude: set[str]) -> list[str]:
    data = tomllib.loads((PROJ_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["tool"]["poetry"]["dependencies"]

    specs: list[str] = []
    for name, decl in deps.items():
        if name.lower() == "python":
            continue  # 解释器版本由 uv 的 --python 参数负责
        if name in exclude:
            print(f"# 按 --exclude 跳过: {name}", file=sys.stderr)
            continue
        if _is_windows_only(decl) and sys.platform != "win32":
            print(f"# 非 Windows 平台跳过: {name}", file=sys.stderr)
            continue
        spec = _to_spec(name, decl)
        if spec:
            specs.append(spec)
    return specs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exclude", action="append", default=[],
                        help="要跳过的依赖名，可重复指定")
    args = parser.parse_args()

    for spec in build_specs(set(args.exclude)):
        print(spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
