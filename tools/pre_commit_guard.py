"""提交前守卫：阻止数据库凭据进入版本库。

【为什么需要】
本仓库是 PUBLIC（github.com/wsh520/stock-fishing-strategy）。凭据一旦进入
commit 历史即不可撤回 —— 即使 force push，GitHub secret scanning 与第三方
爬虫早已抓取。.gitignore 只能挡住「你记得它会忽略」的情况，挡不住
`git add -f`、文件改名、把目录整体挪进另一个路径、打包后解包再提交。
本钩子挡住这些绕过路径。

【检测两类问题】
1. 文件名：.secrets/、.env、*.pem、*.key、id_rsa* 等
2. 内容：MYSQL_* 被赋了非占位值、私钥块、飞书 webhook、云厂商 AK

【安装】
    python tools/pre_commit_guard.py --install

【绕过】
    git commit --no-verify
（不建议；仅当确认是误报时使用）

【自检】
    python tools/pre_commit_guard.py --scan-all
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# ---------------------------------------------------------------- 文件名规则

_BLOCKED_PATH_PARTS = (".secrets/", "/.secrets", "\\secrets\\")
_BLOCKED_NAME_PATTERNS = (
    re.compile(r"(^|/)\.env(\.|$)", re.I),          # .env / .env.local（.env.example 例外）
    re.compile(r"\.(pem|key|p12|pfx|jks)$", re.I),
    re.compile(r"^(id_rsa|id_dsa|id_ecdsa|id_ed25519)", re.I),
    # 只拦「数据文件」形态的凭据文件（credentials.json / .aws/credentials）。
    # 不能宽泛匹配 credential 子串，否则 credential_guard.py 这类代码文件也会被拦，
    # 守卫拦住自己 → 用户被迫长期 --no-verify → 防护形同虚设。
    re.compile(r"^\.?credentials?(\.(json|ya?ml|ini|env|txt|xml|toml|cfg|conf))?$", re.I),
)
# 允许入库的模板文件（不含真实值）
_PATH_ALLOWLIST = (".env.example", ".env.sample", ".env.template")

# ---------------------------------------------------------------- 内容规则

# 键名 + 疑似真实值。值若为占位符/环境变量引用则放行。
_ASSIGN_RE = re.compile(
    r"""^[ \t]*(?:export[ \t]+)?["']?"""
    r"""(MYSQL_PASSWORD|MYSQL_USER|MYSQL_HOST|DB_PASSWORD|DB_USER|DB_HOST)"""
    r"""["']?[ \t]*[=:][ \t]*(.+)$""",
    re.I | re.M,
)

# 值里出现这些子串，说明是引用/占位，不是明文凭据
_PLACEHOLDER_HINTS = (
    "os.environ", "getenv", "${", "{{", "%s", "$(", "\"\"", "''",
    "your_", "your-", "<", "xxx", "***", "changeme", "placeholder",
)


def _looks_like_real_value(raw: str) -> bool:
    """判断 = 右侧是否是真实凭据而非变量引用/占位符。"""
    v = raw.strip().strip(",").strip()
    # 去掉行尾注释
    for marker in (" #", "\t#"):
        if marker in v:
            v = v.split(marker, 1)[0].strip()
    # 去引号
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    if not v:                      # 空值 = 模板，放行
        return False
    low = v.lower()
    if any(h in low for h in _PLACEHOLDER_HINTS):
        return False
    # 太短的不像真密码（如 root 这种用户名仍会被抓，这是有意为之）
    return len(v) >= 4


# 无需键名的硬特征
_HARD_PATTERNS = (
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("飞书 webhook", re.compile(r"open\.feishu\.cn/open-apis/bot/v2/hook/[0-9a-fA-F-]{16,}")),
    ("GitHub token", re.compile(r"\b(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("阿里云 AK", re.compile(r"\bLTAI[A-Za-z0-9]{12,}")),
    ("腾讯云 SecretId", re.compile(r"\bAKID[A-Za-z0-9]{12,}")),
    ("AWS AK", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
)

_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".gz", ".whl", ".exe", ".dll", ".so", ".pyc", ".pkl", ".xlsx", ".xls",
}


# ---------------------------------------------------------------- 扫描核心


def _scan_text(path: str, text: str) -> list[str]:
    """返回该文件命中的问题描述列表。"""
    hits: list[str] = []

    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pat in _HARD_PATTERNS:
            if pat.search(line):
                hits.append(f"  第 {lineno} 行：检测到{label}明文")

    for m in _ASSIGN_RE.finditer(text):
        key, raw = m.group(1), m.group(2)
        if _looks_like_real_value(raw):
            lineno = text[: m.start()].count("\n") + 1
            hits.append(f"  第 {lineno} 行：{key} 被赋了疑似真实值")

    return hits


def _path_problem(path: str) -> str | None:
    norm = path.replace("\\", "/")
    if any(a in norm for a in _PATH_ALLOWLIST):
        return None
    if any(p in norm for p in _BLOCKED_PATH_PARTS):
        return "位于 .secrets/ 私有目录"
    base = norm.rsplit("/", 1)[-1]
    for pat in _BLOCKED_NAME_PATTERNS:
        if pat.search(norm) or pat.search(base):
            return "文件名命中凭据模式"
    return None


# ---------------------------------------------------------------- git 交互


def _git(*args: str) -> str:
    out = subprocess.run(
        ("git",) + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{out.stderr.strip()}")
    return out.stdout


def _staged_files() -> list[str]:
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _staged_content(path: str) -> str:
    """取暂存区（索引）里的内容，而非工作区——工作区内容可能已被改回。"""
    try:
        return _git("show", f":{path}")
    except RuntimeError:
        return ""


def _report(problems: dict[str, list[str]]) -> None:
    total_hits = sum(len(v) for v in problems.values())
    print("=" * 68, file=sys.stderr)
    print("提交被拒绝：检测到疑似凭据/密钥", file=sys.stderr)
    print("=" * 68, file=sys.stderr)
    for path, hits in problems.items():
        print(f"\n  {path}", file=sys.stderr)
        for h in hits:
            print(h, file=sys.stderr)
    print(f"\n共 {len(problems)} 个文件、{total_hits} 处问题。", file=sys.stderr)
    print(
        "\n这是一个 PUBLIC 仓库，凭据一旦进入历史就收不回来。\n"
        "  正确做法：把真实值放在 .secrets/stock-db.env（已被 .gitignore 排除），\n"
        "           代码里只通过 os.environ 读取。\n"
        "  确认是误报：git commit --no-verify",
        file=sys.stderr,
    )
    print("=" * 68, file=sys.stderr)


def check_staged() -> int:
    """返回 0 放行，1 拒绝。"""
    try:
        files = _staged_files()
    except Exception as exc:  # noqa: BLE001 - 守卫必须给出明确失败原因
        print(f"[pre-commit] 无法枚举暂存文件，已阻止提交：{exc}", file=sys.stderr)
        print("[pre-commit] 排查：git status；确需提交用 git commit --no-verify", file=sys.stderr)
        return 1

    problems: dict[str, list[str]] = {}
    for path in files:
        hits: list[str] = []
        p = _path_problem(path)
        if p:
            hits.append(f"  {p}")
        if os.path.splitext(path)[1].lower() not in _BINARY_EXT:
            hits.extend(_scan_text(path, _staged_content(path)))
        if hits:
            problems[path] = hits

    if problems:
        _report(problems)
        return 1

    print(f"[pre-commit] 凭据扫描通过（{len(files)} 个文件）")
    return 0


def scan_all() -> int:
    """扫描全部已跟踪文件（自检用，不涉及暂存区）。"""
    files = [f for f in _git("ls-files").splitlines() if f.strip()]
    problems: dict[str, list[str]] = {}
    for path in files:
        if os.path.splitext(path)[1].lower() in _BINARY_EXT:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        hits = _scan_text(path, text)
        if hits:
            problems[path] = hits

    if problems:
        _report(problems)
        return 1
    print(f"[scan-all] {len(files)} 个已跟踪文件，未发现凭据")
    return 0


def install() -> int:
    hooks_dir = _git("rev-parse", "--git-path", "hooks").strip()
    hook_path = os.path.join(hooks_dir, "pre-commit")
    py = sys.executable.replace("\\", "/")

    if os.path.exists(hook_path):
        with open(hook_path, "r", encoding="utf-8", errors="replace") as fh:
            old = fh.read()
        if "pre_commit_guard" not in old:
            # 只备份「别人的」钩子；自己的钩子直接幂等覆盖，避免升级后残留旧内容
            backup = hook_path + ".bak"
            with open(backup, "w", encoding="utf-8") as fh:
                fh.write(old)
            print(f"[install] 已备份原有钩子到 {backup}")

    body = (
        "#!/bin/sh\n"
        "# 由 tools/pre_commit_guard.py --install 生成\n"
        "# 用 toplevel 定位脚本，项目整体搬家后钩子仍然有效\n"
        'root=$(git rev-parse --show-toplevel) || exit 1\n'
        f'exec "{py}" "$root/tools/pre_commit_guard.py"\n'
    )
    with open(hook_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    os.chmod(hook_path, 0o755)
    print(f"[install] 已安装 pre-commit 钩子 -> {hook_path}")
    print("[install] 自检：python tools/pre_commit_guard.py --scan-all")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="阻止凭据进入 git 版本库")
    ap.add_argument("--install", action="store_true", help="安装到 .git/hooks/pre-commit")
    ap.add_argument("--scan-all", action="store_true", help="扫描全部已跟踪文件（自检）")
    args = ap.parse_args()

    if args.install:
        return install()
    if args.scan_all:
        return scan_all()
    return check_staged()


if __name__ == "__main__":
    sys.exit(main())
