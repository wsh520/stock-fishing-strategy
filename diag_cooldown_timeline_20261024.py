"""拉取 daily_screen 最近 N 次已完成运行的日志，解析出：
- 每次运行的信号日（rec_date）
- 正式推荐（name/code/score）
- 冷却压制事件（QV_COOLDOWN：资格通过但被 10 日冷却挡下的股票）
- 冷却窗口起点（since_date）

只读远程 API + 本地落盘 /tmp/gh_logs，不碰仓库。
"""
import json
import os
import re
import subprocess
import sys
import zipfile
import io

REPO = "wsh520/stock-fishing-strategy"
WORKFLOW = "daily_screen.yml"
OUT = "/tmp/gh_logs"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def token():
    out = sh('printf "protocol=https\\nhost=github.com\\n\\n" | git credential fill')
    m = re.search(r"^password=(.+)$", out, re.M)
    return m.group(1).strip() if m else None


def api(path, tok):
    r = subprocess.run(
        ["curl", "-s", "-H", f"Authorization: token {tok}",
         f"https://api.github.com/repos/{REPO}/{path}"],
        capture_output=True, text=True)
    return json.loads(r.stdout)


def main():
    tok = token()
    assert tok, "no github token"
    runs = api(f"actions/workflows/{WORKFLOW}/runs?per_page=30", tok)["workflow_runs"]
    done = [r for r in runs if r["conclusion"] == "success" and r["event"] in ("schedule", "workflow_dispatch")]
    print(f"已完成运行 {len(done)} 个（schedule+dispatch）")

    os.makedirs(OUT, exist_ok=True)
    manifest = []
    for r in done:
        rid, num = r["id"], r["run_number"]
        zpath = os.path.join(OUT, f"run_{rid}.zip")
        if not os.path.exists(zpath) or os.path.getsize(zpath) == 0:
            r2 = subprocess.run(
                ["curl", "-sL", "-H", f"Authorization: token {tok}",
                 f"https://api.github.com/repos/{REPO}/actions/runs/{rid}/logs", "-o", zpath],
                capture_output=True, text=True)
        try:
            with zipfile.ZipFile(zpath) as z:
                text = z.read("0_screen.txt").decode("utf-8", "replace")
        except Exception as exc:
            print(f"  run #{num}: 日志读取失败 {exc}")
            continue
        text = re.sub(r"^[0-9T:.-]+Z ", "", text, flags=re.M)
        text = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", "", text, flags=re.M)

        rec = {"run": num, "id": rid, "created_at": r["created_at"], "event": r["event"]}
        m = re.search(r"重复推荐冷却：已查库，since_date=(\d{4}-\d{2}-\d{2})（(\d+) 个交易日窗口），命中历史推荐 (\d+) 只", text)
        if m:
            rec["since_date"] = m.group(1)
            rec["cooldown_days"] = int(m.group(2))
            rec["history_hits"] = int(m.group(3))
        rec["formal"] = [
            {"code": c, "name": n, "score": s}
            for n, c, s in re.findall(r"推荐: ([^\(]+)\((\d{6})\) 评分 ([\d.]+)", text)
        ]
        rec["cooldown_blocked"] = [
            {"code": c, "name": n}
            for n, c in re.findall(r"\[QV_COOLDOWN\] ([^\(]+)\((\d{6})\) \| 资格=passed \| 推送=cooldown", text)
        ]
        m = re.search(r"行情日期=(\d{4}-\d{2}-\d{2})", text)
        rec["signal_date"] = m.group(1) if m else None
        # 旧 run 无「行情日期」字段时，落库 rec_date 用的是快照日（数据截止日），
        # 不是运行日——假期 run（如 #92/#94）快照日=2026-09-30（DB 去重后只留 1 行）。
        m = re.search(r"快照日 (\d{4}-\d{2}-\d{2})", text)
        rec["snapshot_date"] = m.group(1) if m else None
        rec["rec_date"] = rec["signal_date"] or rec["snapshot_date"]
        # 落库结果（新增/跳过/失败）——#101 曾 signals_hit 列溢出导致推荐没进 DB
        m = re.search(r"推荐结果已落库\[bottom_fishing\]：新增 (\d+) 条（共提交 (\d+) 条，跳过 (\d+) 条未过期记录）", text)
        if m:
            rec["db_saved"] = {"new": int(m.group(1)), "submitted": int(m.group(2)), "skipped": int(m.group(3))}
        elif "落库失败" in text:
            rec["db_saved"] = {"failed": True}
        m = re.search(r"最终分层：候选 (\d+)，资格通过 (\d+)，冷却转出 (\d+)，普通待核验 (\d+)，观察合计 (\d+)，冷却后正式 (\d+)", text)
        if m:
            rec["funnel"] = {"candidates": int(m.group(1)), "qualified": int(m.group(2)),
                             "cooled": int(m.group(3)), "pending": int(m.group(4)),
                             "formal_after_cool": int(m.group(6))}
        # no_save 真值判定：GitHub 注解行以 ::notice:: 开头；日志里同时存在
        # workflow 脚本源码的 echo "::notice::已勾选 no_save..." 回显（cyan），
        # 必须剥 ANSI 色后按「行首是 ::notice:: 且含 no_save」匹配，否则全量假阳性。
        plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
        rec["no_save"] = any(
            ln.startswith("::notice::") and "no_save" in ln
            for ln in plain.splitlines()
        )
        manifest.append(rec)
        print(f"  #{num} {rec['event'][:4]} 信号日={rec['signal_date']} 快照日={rec.get('snapshot_date')} "
              f"formal={len(rec['formal'])} cooled={len(rec['cooldown_blocked'])} "
              f"db={rec.get('db_saved')} no_save={rec['no_save']}")

    with open(os.path.join(OUT, "cooldown_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print("写出 cooldown_manifest.json")


if __name__ == "__main__":
    main()
