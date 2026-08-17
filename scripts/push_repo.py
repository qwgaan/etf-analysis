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
import time
import urllib.request
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
    """回退:递归扫描,跳过 .git / 运行时目录。"""
    skip = {".git", "__pycache__", ".vscode", "node_modules", "data", "outputs"}
    out: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in skip for part in p.relative_to(root).parts):
            continue
        out.append(str(p.relative_to(root)).replace("\\", "/"))
    return out


def _api_json(req: urllib.request.Request, retries: int = 4) -> dict:
    """发送请求并返回 JSON,HTTP 错误时解析响应体里的 message。

    对 SSL 连接中断(UNEXPECTED_EOF)/限流(403/429)做指数退避重试,
    避免沙箱代理对连续 PUT 不稳定导致偶尔失败。
    """
    req.add_header("Connection", "close")  # 禁用 keep-alive,规避复用连接被重置
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            d = json.loads(e.read().decode("utf-8") or "{}")
            msg = d.get("message", f"HTTP {e.code}")
            # 限流时退避重试
            if e.code in (403, 429) and attempt < retries - 1:
                time.sleep(3 * (2 ** attempt))
                last = RuntimeError(msg)
                continue
            raise RuntimeError(msg)
        except urllib.error.URLError as e:
            last = RuntimeError(f"网络错误: {e}")
            if attempt < retries - 1:
                time.sleep(3 * (2 ** attempt))
                continue
            raise last
    if last:
        raise last
    raise RuntimeError("未知网络错误")


def _get_file_sha(owner: str, repo: str, path: str, branch: str) -> str | None:
    """获取远端文件 blob sha;不存在则返回 None。"""
    url = f"{API}/repos/{owner}/{repo}/contents/{path}?ref={branch}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        d = _api_json(req)
        if isinstance(d, dict):
            return d.get("sha")
        if isinstance(d, list):
            return None
    except RuntimeError as e:
        if "Not Found" in str(e):
            return None
        raise
    return None


def _put_file(owner: str, repo: str, branch: str, path: str, content: bytes, message: str) -> None:
    """用 urllib 直接 PUT(Content 走请求体,不受命令行长度限制)。
    文件已存在时自动带 sha 更新。"""
    sha = _get_file_sha(owner, repo, path, branch)
    url = f"{API}/repos/{owner}/{repo}/contents/{path}"
    payload = {
        "message": message,
        "branch": branch,
        "content": base64.b64encode(content).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Content-Type", "application/json")
    d = _api_json(req)
    if "message" in d and not (d.get("content") or d.get("commit")):
        raise RuntimeError(d["message"])


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 1
    owner, repo, local = sys.argv[1], sys.argv[2], sys.argv[3]
    branch = sys.argv[4] if len(sys.argv) > 4 else "main"
    message = sys.argv[5] if len(sys.argv) > 5 else None

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

    default_msg = os.environ.get("GH_PUSH_MESSAGE", "chore: push {rel}")
    print(f"[push] 目标: {owner}/{repo}@{branch} · {len(files)} 个文件")
    ok = fail = 0
    for i, rel in enumerate(files, 1):
        p = root / rel
        try:
            content = p.read_bytes()
            msg = message or default_msg.format(rel=rel)
            _put_file(owner, repo, branch, rel, content, msg)
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