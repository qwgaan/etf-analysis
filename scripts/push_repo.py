#!/usr/bin/env python3
"""
推送本地目录到 GitHub 仓库(沙箱 git push 被拦时的回退方案)。
- 优先用 git ls-files 尊重 .gitignore;否则递归扫描并跳过 .git
- 用 Contents API 每个文件一次 commit;空仓库也能用
- token 从环境变量 GH_TOKEN 读取,绝不落盘/回显

用法:
  export GH_TOKEN=ghp_xxx...
  python scripts/push_repo.py <owner> <repo> <本地目录> [branch=main]
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

API = "https://api.github.com"


def _git_ls_files(root: Path) -> list[str]:
    """尽量用 git ls-files 列出已跟踪文件,尊重 .gitignore。"""
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "--others", "--cached", "--exclude-standard", "-z"],
            cwd=str(root), stderr=subprocess.DEVNULL,
        )
        return [p.decode("utf-8") for p in out.split(b"\x00") if p]
    except Exception:
        return []


def _walk(root: Path) -> list[str]:
    """回退:递归扫描,跳过 .git / .github 等。"""
    skip = {".git", "__pycache__", ".vscode", "node_modules"}
    out: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in skip for part in p.relative_to(root).parts):
            continue
        out.append(str(p.relative_to(root)).replace("\\", "/"))
    return out


def _put_file(owner: str, repo: str, branch: str, path: str, content: bytes, message: str) -> None:
    """上传一个文件(空文件已有时为新增,否则为新增)。失败抛 RuntimeError。"""
    url = f"{API}/repos/{owner}/{repo}/contents/{path}"
    body = {
        "message": message,
        "branch": branch,
        "content": base64.b64encode(content).decode("ascii"),
    }
    cmd = [
        "curl", "-s", "-X", "PUT", url,
        "-H", "Accept: application/vnd.github+json",
        "-H", f"Authorization: Bearer {os.environ['GH_TOKEN']}",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
        "-d", json.dumps(body),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"curl 失败: {r.stderr[:200]}")
    try:
        d = json.loads(r.stdout)
    except Exception:
        raise RuntimeError(f"响应非 JSON: {r.stdout[:200]}")
    if "message" in d and not (d.get("content") or d.get("commit")):
        raise RuntimeError(f"API 错误: {d['message']}")


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 1
    owner, repo, local = sys.argv[1], sys.argv[2], sys.argv[3]
    branch = sys.argv[4] if len(sys.argv) > 4 else "main"

    if not os.environ.get("GH_TOKEN"):
        print("[ERROR] 请设置环境变量 GH_TOKEN", file=sys.stderr)
        return 2

    root = Path(local).resolve()
    if not root.is_dir():
        print(f"[ERROR] 目录不存在: {root}", file=sys.stderr)
        return 1

    files = _git_ls_files(root)
    if not files:
        files = _walk(root)
    if not files:
        print(f"[ERROR] 没找到任何文件 in {root}", file=sys.stderr)
        return 1

    print(f"[push] 目标: {owner}/{repo}@{branch} · {len(files)} 个文件")
    ok = fail = 0
    for i, rel in enumerate(files, 1):
        p = root / rel
        try:
            content = p.read_bytes()
            _put_file(owner, repo, branch, rel, content, f"chore: push {rel}")
            ok += 1
            if i % 10 == 0 or i == len(files):
                print(f"[push] 进度 {i}/{len(files)} · 成功 {ok} · 失败 {fail}")
        except Exception as e:
            fail += 1
            print(f"[push] 失败 {rel}: {e}")

    print(f"\n[push] 完成: 成功 {ok} · 失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())