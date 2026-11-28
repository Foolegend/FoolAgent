#!/usr/bin/env python3
"""skills.py — 技能发现：扫描工作区里的技能包，生成给模型看的索引。

一个技能包就是一个目录：

    .fool/skills/<名字>/
      SKILL.md            # 说明：什么时候用、怎么用（frontmatter 里写 name/description）
      scripts/...         # 可直接运行的脚本
      references/...      # 参考资料

这里只做一件事：把技能索引拼成一段话塞进 system prompt，
让模型知道自己有哪些现成的 CLI 可以调。

工具数量不变——还是 read / write / edit / bash 四个，
模型要跑技能脚本，照旧用 bash 调。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 技能包所在目录（相对工作区根目录）
SKILLS_DIR = Path(".fool") / "skills"
# 技能说明文件
SKILL_FILE = "SKILL.md"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


@dataclass
class Skill:
    """一个技能包。"""

    name: str
    description: str
    manifest: str = ""        # SKILL.md 的相对路径
    scripts: list = field(default_factory=list)
    references: list = field(default_factory=list)


def _parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md 头部的 frontmatter。

    只认 name 和 description 两个键（不引 yaml 依赖）：
    值可以跨行续写，缩进的部分接在上一行后面。
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}

    values: dict = {}
    key = None
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if line[:1] in (" ", "\t"):  # 续行
            if key:
                values[key] = f"{values[key]} {line.strip()}".strip()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        values[key] = value.strip().strip('"').strip("'")
    return values


def _relative(path: Path, base: Path) -> str:
    """相对工作区根目录的路径，统一用 / 分隔。"""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _list_files(directory: Path, base: Path) -> list:
    """列出目录下的文件（只取一层，不递归）。"""
    if not directory.is_dir():
        return []
    return sorted(
        _relative(item, base)
        for item in directory.iterdir()
        if item.is_file()
    )


def find_skills(base=None, skills_dir=None) -> list:
    """扫描技能目录，返回技能列表（按名字排序）。

    没有技能目录、或目录是空的，都返回空列表——这是正常情况，不是错误。
    """
    root = Path(base) if base is not None else Path.cwd()
    directory = root / (skills_dir if skills_dir is not None else SKILLS_DIR)
    if not directory.is_dir():
        return []

    skills = []
    for entry in sorted(directory.iterdir()):
        manifest = entry / SKILL_FILE
        if not entry.is_dir() or not manifest.is_file():
            continue
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        values = _parse_frontmatter(text)
        skills.append(Skill(
            name=values.get("name") or entry.name,
            description=values.get("description") or "",
            manifest=_relative(manifest, root),
            scripts=_list_files(entry / "scripts", root),
            references=_list_files(entry / "references", root),
        ))
    return skills


def format_index(skills) -> str:
    """把技能列表拼成给模型看的索引段落。

    刻意写得很短：只给名字、用途、说明文件位置和能直接跑的脚本，
    细节让模型自己 read 说明文件。
    """
    if not skills:
        return ""

    lines = ["可用技能（从工作区的技能包里发现的）："]
    for skill in skills:
        lines.append(f"- {skill.name}：{skill.description}")
        lines.append(f"  说明文件：{skill.manifest}")
        if skill.scripts:
            lines.append(f"  可以直接跑的脚本：{'、'.join(skill.scripts)}")
        if skill.references:
            lines.append(f"  参考资料：{'、'.join(skill.references)}")
    lines.append(
        "需要某个技能时，先 read 它的说明文件，再按说明用 bash 调它的脚本；"
        "不要自己另写一份。"
    )
    return "\n".join(lines)


def index_for(base=None, skills_dir=None) -> str:
    """一步到位：扫描 + 拼索引。没有技能时返回空字符串。"""
    return format_index(find_skills(base, skills_dir))
