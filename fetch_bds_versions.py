#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BDS 版本采集器 —— 三层策略获取 Minecraft Bedrock Dedicated Server 下载链接。

背景
====
旧版 scan_new.py 只做「版本号暴力嗅探」：从已知最新版本往后枚举
patch/build 组合逐个 HEAD 探测。致命缺陷是 **不会跨 minor**：
官方发新大版本（如 1.26 → 1.27）后，patch 号会跳到 60+/70+，
嗅探范围完全够不着，导致列表长期落后（实测落后 27 个 patch）。

本脚本改用三层策略
==================
  Layer 1  官网 / wiki 抓取   minecraft.wiki/w/Bds 的版本历史表，
                               一次请求拿到全部候选版本号（实测 363 个）
  Layer 2  并发双通道 HEAD    对候选号分别探测 stable / preview 两个
                               下载路径，确认真实存在的那个并记下 channel
  Layer 3  版本号嗅探兜底     Layer 1 没覆盖到的新 minor 时，
                               按版本号规律外扩枚举（支持跨 minor）

用法
====
    python fetch_bds_versions.py                  # 增量采集并写入
    python fetch_bds_versions.py --dry            # 只预览不写入
    python fetch_bds_versions.py --full           # 全量重新验证
    python fetch_bds_versions.py --source wiki    # 只用 wiki 抓取
    python fetch_bds_versions.py --source sniff   # 只用嗅探
    python fetch_bds_versions.py --workers 16     # 并发数（默认 12）
    python fetch_bds_versions.py --timeout 8      # 单次 HEAD 超时秒数

设计要点
========
  * 只做增量：新版本才探测，旧条目原样保留，采集失败不会丢数据
  * 并发探测 + 429 指数退避，避免被 CDN 限速
  * wiki 需要完整请求头（Accept-Language / Accept-Encoding），
    否则 403 —— 这是实测踩过的坑
  * 输出格式与旧版 bds_versions.json 完全兼容，主程序无感升级
"""

import argparse
import gzip
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION_FILE = "bds_versions.json"

# ── 下载地址模板 ──
STABLE_URL_FMT = "https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-{version}.zip"
PREVIEW_URL_FMT = "https://www.minecraft.net/bedrockdedicatedserver/bin-win-preview/bedrock-server-{version}.zip"

# ── 候选来源 ──
WIKI_URL = "https://minecraft.wiki/w/Bds"
# 官方更新日志（备用源，wiki 挂了时用）
CHANGELOG_URL = "https://www.minecraft.net/en-us/article/minecraft-bedrock-dedicated-server-updates"

# 完整请求头 —— wiki 缺 Accept-Language / Accept-Encoding 会直接 403
WIKI_HEADERS = {
    "User-Agent": "BDS-Manager/3.5 (https://github.com/TussalZeus18028/bds_manager)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
    "Connection": "close",
}
PROBE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/125.0.2535.92",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
]

# 版本号：1.26.70.26 —— 允许 patch/build 两位
VERSION_RE = re.compile(r"\b(1\.\d{1,2}\.\d{1,2}\.\d{1,2})\b")


# ══════════════════════════════════════════════════════════════
#  工具
# ══════════════════════════════════════════════════════════════

def vkey(s: str) -> tuple:
    """'1.26.70.26' → (1, 26, 70, 26)，用于排序比较。"""
    parts = [int(x) for x in s.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def _fetch(url: str, headers: dict, timeout: float = 30.0) -> str:
    """抓取网页文本，自动处理 gzip。"""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


# ══════════════════════════════════════════════════════════════
#  Layer 1 — 候选版本号抓取
# ══════════════════════════════════════════════════════════════

def fetch_from_wiki(timeout: float = 30.0) -> list[str]:
    """从 minecraft.wiki 的 BDS 版本历史页提取版本号。"""
    try:
        html = _fetch(WIKI_URL, WIKI_HEADERS, timeout)
    except Exception as e:
        print(f"  ⚠️ wiki 抓取失败: {type(e).__name__}: {e}")
        return []
    vers = sorted(set(VERSION_RE.findall(html)), key=vkey, reverse=True)
    print(f"  ✅ wiki 抓到 {len(vers)} 个候选版本 (最新 {vers[0] if vers else 'N/A'})")
    return vers


def fetch_from_changelog(timeout: float = 30.0) -> list[str]:
    """从官方更新日志页提取版本号（备用源）。"""
    try:
        html = _fetch(CHANGELOG_URL, PROBE_HEADERS, timeout)
    except Exception as e:
        print(f"  ⚠️ 更新日志抓取失败: {type(e).__name__}: {e}")
        return []
    vers = sorted(set(VERSION_RE.findall(html)), key=vkey, reverse=True)
    print(f"  ✅ 更新日志抓到 {len(vers)} 个候选版本")
    return vers


# ══════════════════════════════════════════════════════════════
#  Layer 2 — 并发双通道 HEAD 验证
# ══════════════════════════════════════════════════════════════

def probe_branch(version: str, branch: str, timeout: float = 8.0) -> dict | None:
    """对单个分支做 HEAD 探测，成功返回条目，失败返回 None。"""
    url = (STABLE_URL_FMT if branch == "stable" else PREVIEW_URL_FMT).format(version=version)
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, method="HEAD",
                headers={"User-Agent": random.choice(UA_POOL), "Accept": "*/*"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.getcode() == 200:
                    return {
                        "version": version,
                        "branch": branch,
                        "url": url,
                        "size": int(resp.headers.get("content-length", 0) or 0),
                    }
                return None
        except urllib.error.HTTPError as e:
            if e.code == 429:                      # 被限速 → 指数退避
                time.sleep(min(2 ** attempt, 10))
                continue
            return None                            # 404 等 → 该分支不存在
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
            else:
                return None
    return None


def verify_version(version: str, timeout: float = 8.0) -> dict | None:
    """验证单个版本号：stable 优先，其次 preview。"""
    for branch in ("stable", "preview"):
        entry = probe_branch(version, branch, timeout)
        if entry:
            return entry
    return None


def verify_many(versions: list[str], workers: int = 12, timeout: float = 8.0,
                quiet: bool = False) -> list[dict]:
    """并发验证一批版本号。"""
    if not versions:
        return []
    found: list[dict] = []
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(verify_version, v, timeout): v for v in versions}
        for fut in as_completed(futures):
            done += 1
            entry = fut.result()
            if entry:
                found.append(entry)
            if not quiet and (done % 20 == 0 or done == len(versions)):
                print(f"    进度 {done}/{len(versions)}  命中 {len(found)}  "
                      f"({time.time() - t0:.0f}s)")
    return found


# ══════════════════════════════════════════════════════════════
#  Layer 3 — 版本号嗅探兜底
# ══════════════════════════════════════════════════════════════

def sniff_candidates(latest: str, patch_span: int = 5, build_span: int = 20,
                     minors: int = 1) -> list[str]:
    """从已知最新版本外扩枚举候选版本号。

    相比旧 scan_new.py 的改进：**支持跨 minor** —— 官方发大版本时
    patch 号会跳到 60+/70+，只探同一 minor 永远追不上。

    范围刻意收紧（几百个而非几千个），因为这一层只是兜底：
    补 wiki 尚未同步的新版本，不需要全量枚举。
    """
    major, minor, patch, build = vkey(latest)
    out: list[str] = []
    # 同 patch 下更大的 build
    for bb in range(build + 1, build + 1 + build_span):
        out.append(f"{major}.{minor}.{patch}.{bb}")
    # 近几个 patch
    for pp in range(patch + 1, patch + 1 + patch_span):
        for bb in range(0, build_span):
            out.append(f"{major}.{minor}.{pp}.{bb}")
    # 下一个 minor 的开头几个 patch（新大版本首发通常是 patch=0/1）
    for pp in range(0, 4):
        for bb in range(0, build_span):
            out.append(f"{major}.{minor + 1}.{pp}.{bb}")
    return out


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════

def existing_versions_of(data: dict) -> list[str]:
    """从已有数据里取出版本号列表。"""
    return [v["version"] for v in data.get("versions", [])]


def load_existing(path: str) -> dict:
    if not os.path.exists(path):
        return {"name": "Minecraft Bedrock Dedicated Server Versions",
                "description": "BDS 版本列表，由 BDS Manager 维护",
                "updated": "", "versions": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser(description="BDS 版本采集器")
    ap.add_argument("--file", default=VERSION_FILE, help="版本列表文件")
    ap.add_argument("--dry", action="store_true", help="只预览不写入")
    ap.add_argument("--full", action="store_true", help="全量重新验证（旧条目也探测）")
    ap.add_argument("--source", choices=["wiki", "sniff", "both"], default="both")
    ap.add_argument("--workers", type=int, default=12, help="并发探测数")
    ap.add_argument("--timeout", type=float, default=8.0, help="单次 HEAD 超时秒数")
    args = ap.parse_args()

    data = load_existing(args.file)
    existing = data.get("versions", [])
    have = {v["version"] for v in existing}
    print(f"当前 {args.file}: {len(existing)} 个版本 (updated={data.get('updated','')})")

    # ── 收集候选 ──
    candidates: list[str] = []
    if args.source in ("wiki", "both"):
        print("\n[Layer 1] 抓取候选版本号...")
        got = fetch_from_wiki()
        if not got:
            print("  wiki 不可用，尝试官方更新日志...")
            got = fetch_from_changelog()
        candidates.extend(got)

    if args.source in ("sniff", "both"):
        print("\n[Layer 3] 版本号嗅探兜底...")
        # 基准取所有已知候选里最高的版本（wiki 抓到的最新优先）
        known = existing_versions_of(data) + candidates
        base = max(known, key=vkey) if known else "1.26.43.1"
        sn = sniff_candidates(base)
        candidates.extend(sn)
        print(f"  从 {base} 外扩出 {len(sn)} 个候选 (含跨 minor)")

    # 去重 + 过滤
    uniq = sorted(set(candidates), key=vkey, reverse=True)
    if args.full:
        todo = uniq
        print(f"\n全量模式：验证全部 {len(todo)} 个候选")
    else:
        todo = [v for v in uniq if v not in have]
        print(f"\n增量模式：{len(todo)} 个新候选待验证（跳过 {len(uniq) - len(todo)} 个已知）")

    if not todo:
        print("没有新版本需要验证。")
        return 0

    # ── Layer 2 验证 ──
    print(f"\n[Layer 2] 并发验证 ({args.workers} 线程)...")
    t0 = time.time()
    found = verify_many(todo, args.workers, args.timeout)
    print(f"  用时 {time.time() - t0:.0f}s，命中 {len(found)} 个")

    if not found:
        print("没有发现新版本。")
        return 0

    stable = [e for e in found if e["branch"] == "stable"]
    preview = [e for e in found if e["branch"] == "preview"]
    print(f"  stable {len(stable)} 个, preview {len(preview)} 个")
    for e in sorted(found, key=lambda x: vkey(x["version"]), reverse=True)[:15]:
        print(f"    {e['branch']:8s} {e['version']:14s} {e['size'] / 1048576:6.1f} MB")

    if args.dry:
        print("\n(--dry 仅预览，未写入)")
        return 0

    # ── 合并写入 ──
    merged = {v["version"]: v for v in existing}
    for e in found:
        merged[e["version"]] = {"version": e["version"], "branch": e["branch"], "url": e["url"]}
    all_versions = sorted(merged.values(), key=lambda x: vkey(x["version"]), reverse=True)
    data["versions"] = all_versions
    data["updated"] = time.strftime("%Y-%m-%d")

    tmp = args.file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, args.file)
    print(f"\n✅ 已更新 {args.file}: {len(existing)} → {len(all_versions)} 个版本")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
