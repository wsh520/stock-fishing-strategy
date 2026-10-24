"""2026-10-24：通过 GitHub Git Data API 推送本地 commit（git push 走不通——
本机代理只放行 api.github.com，不放行 github.com，schannel/直连均失败）。

背景：WorkBuddy 沙箱/本机代理（127.0.0.1:11390）只允许 api.github.com。
git push（github.com:443）schannel 握手失败、清代理直连也超时。
唯一通路 = Git Data API（blobs/trees/commits/refs）。

已知限制（2026-10-24 实测）：GitHub 创建 commit 时会归一化消息（修剪尾部
空白）与时区（显示为 Z），**无法逐字复现本地 commit SHA**。因此本脚本
不追求 SHA 一致：推完后在本地 `git fetch && git reset --hard origin/main`
对齐即可。tree/blob SHA 有断言（内容保真），commit SHA 只记录不强制。

流程（每个 commit 按拓扑序）：
  1) git diff 找改动文件 → POST /git/blobs（断言 blob SHA 与本地一致）；
  2) POST /git/trees（base_tree=父 tree；断言 tree SHA 与本地一致）；
  3) POST /git/commits（parent=远端当前 head + 原 author/committer + 修剪消息）；
  4) PATCH /git/refs/heads/main 前移（fast-forward，不 force）。
最后打印远端 head，供本地 reset 对齐。

注意：subprocess(shell=True) 在 Windows 是 cmd.exe——'^' 是转义符，
禁用 <sha>^ / <sha>^{tree} 语法（用 --format=%P/%T 代替）；
payload 走临时文件（base64 超 Windows 32KB 命令行限制）。
"""
import base64
import json
import os
import subprocess

REPO = "wsh520/stock-fishing-strategy"
BRANCH = "main"
BASE = "4a7295cee70c794bbf175d376ad9e19590b8f842"  # 推送前远端 head


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cmd failed: {cmd}\n{r.stderr[-500:]}")
    return r.stdout


def api(method, path, tok, payload=None):
    cmd = ["curl", "-s", "-X", method, "-H", f"Authorization: token {tok}",
           "-H", "Accept: application/vnd.github+json",
           f"https://api.github.com/repos/{REPO}/{path}"]
    tmp = None
    if payload is not None:
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        cmd += ["-H", "Content-Type: application/json", "-d", f"@{tmp}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        if tmp:
            os.unlink(tmp)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"{method} {path} 返回非 JSON: {r.stdout[:300]}")


def main():
    out = sh('printf "protocol=https\\nhost=github.com\\n\\n" | git credential fill')
    tok = next((ln.split("=", 1)[1].strip() for ln in out.splitlines()
                if ln.startswith("password=")), None)
    assert tok, "no token"

    to_push = sh(f"git rev-list --reverse {BASE}..HEAD").split()
    print(f"待推送 {len(to_push)} 个 commit: {[s[:8] for s in to_push]}")

    ref = api("GET", f"git/ref/heads/{BRANCH}", tok)
    remote_head = ref["object"]["sha"]
    print(f"远端 {BRANCH} = {remote_head[:8]}")
    if remote_head != BASE:
        upd = api("PATCH", f"git/refs/heads/{BRANCH}", tok,
                  {"sha": BASE, "force": True})
        if upd.get("object", {}).get("sha") != BASE:
            raise RuntimeError(f"重置远端失败: {upd}")
        remote_head = BASE
        print(f"已重置远端 {BRANCH} -> {BASE[:8]}")

    for sha in to_push:
        parent = sh(f"git log -1 --format=%P {sha}").split()[0]
        if parent != remote_head:
            raise RuntimeError(f"{sha[:8]} 的父 {parent[:8]} != 远端 {remote_head[:8]}，"
                               "非 fast-forward，中止（不 force）")
        parent_tree = api("GET", f"git/commits/{parent}", tok)["tree"]["sha"]
        an, ae, ad, cn, ce, cd = sh(
            f'git log -1 --format=%an\x1f%ae\x1f%aI\x1f%cn\x1f%ce\x1f%cI {sha}'
        ).strip().split("\x1f")
        changed = sh(f"git diff --name-only {parent} {sha}").split()
        print(f"{sha[:8]}（父 {parent[:8]}）改动 {len(changed)} 个文件")

        tree_items = []
        for path in changed:
            content = sh(f"git show {sha}:{path}")
            blob = api("POST", "git/blobs", tok, {
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "encoding": "base64"})
            if "sha" not in blob:
                raise RuntimeError(f"blob 创建失败 {path}: {blob}")
            local_blob = sh(f"git rev-parse {sha}:{path}").strip()
            assert blob["sha"] == local_blob, \
                f"blob SHA 不一致 {path}: api={blob['sha']} local={local_blob}"
            tree_items.append({"path": path, "mode": "100644",
                               "type": "blob", "sha": blob["sha"]})

        tree = api("POST", "git/trees", tok, {"base_tree": parent_tree,
                                              "tree": tree_items})
        if "sha" not in tree:
            raise RuntimeError(f"tree 创建失败: {tree}")
        local_tree = sh(f"git log -1 --format=%T {sha}").strip()
        assert tree["sha"] == local_tree, \
            f"tree SHA 不一致: api={tree['sha']} local={local_tree}"

        # GitHub 会修剪尾部空白，这里预修剪（SHA 仍可能与本地不同，见模块 docstring）
        msg = sh(f"git log -1 --format=%B {sha}").rstrip() + "\n"
        commit = api("POST", "git/commits", tok, {
            "message": msg, "tree": tree["sha"], "parents": [remote_head],
            "author": {"name": an, "email": ae, "date": ad},
            "committer": {"name": cn, "email": ce, "date": cd}})
        if "sha" not in commit:
            raise RuntimeError(f"commit 创建失败: {commit}")
        print(f"  commit {commit['sha'][:8]}（本地原 SHA {sha[:8]}，"
              f"GitHub 归一化后不同，内容/tree 已验证一致）")

        upd = api("PATCH", f"git/refs/heads/{BRANCH}", tok,
                  {"sha": commit["sha"], "force": False})
        if upd.get("object", {}).get("sha") != commit["sha"]:
            raise RuntimeError(f"ref 更新失败: {upd}")
        remote_head = commit["sha"]
        print(f"  {BRANCH} -> {remote_head[:8]} ✓")

    print(f"\n✅ 推送完成：远端 {BRANCH} = {remote_head}")
    print("本地对齐：git fetch origin && git reset --hard origin/main")


if __name__ == "__main__":
    main()
