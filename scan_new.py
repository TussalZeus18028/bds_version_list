# -*- coding: utf-8 -*-
"""
BDS 版本扫描器 —— 从最新版本向后探测新的 stable / preview 版本。

用法：
    python scan_new.py              # 扫描并更新
    python scan_new.py --dry         # 只扫描不写入
"""

import json, sys, time, random, socket, ssl
import urllib.request, urllib.error

VERSION_FILE = "bds_versions.json"
STABLE_URL_FMT = "https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-{version}.zip"
PREVIEW_URL_FMT = "https://www.minecraft.net/bedrockdedicatedserver/bin-win-preview/bedrock-server-{version}.zip"
RANGE_SMALL = {"patch": 15, "build": 45}
RANGE_BIG = {"patch": 60, "build": 80}

# User-Agent 轮换池
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/124.0.2478.105",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/125.0.2535.92",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
]


def probe_head(version: str, branch: str, timeout: float = 8.0) -> bool:
    """对单个 URL 做 HEAD 探测，支持 3 次重试 + exponential backoff。"""
    url_fmt = STABLE_URL_FMT if branch == "stable" else PREVIEW_URL_FMT
    url = url_fmt.format(version=version)
    for attempt in range(3):
        try:
            ssl_ctx = ssl.create_default_context()
            req = urllib.request.Request(url, method="HEAD",
                headers={"User-Agent": random.choice(UA_POOL)})
            resp = urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx)
            if resp.getcode() == 200:
                return True
            return False
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(2 ** attempt, 10)
                print(f"    ⚠️ 429 限速，等待 {wait}s...")
                time.sleep(wait)
            else:
                return False
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
            else:
                return False
    return False


def parse_version(s: str) -> tuple:
    """'1.26.33.1' → (1, 26, 33, 1)。"""
    parts = [int(x) for x in s.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def generate_urls(start_version: str, branch: str, scan_depth: str = "small") -> list[str]:
    """从 start_version 向后续版本生成待探测列表。patch/build 范围是相对于起点的偏移量。"""
    major, minor, patch, build = parse_version(start_version)
    versions = []
    r = RANGE_SMALL if scan_depth == "small" else RANGE_BIG
    # 同一 patch 号下的更大 build 号
    for bp in range(build + 1, build + 1 + r["build"]):
        v = f"{major}.{minor}.{patch}.{bp}"
        versions.append(v)
    # 更高 patch 号下的所有 build
    for pp in range(patch + 1, patch + 1 + r["patch"]):
        for b in range(0, r["build"]):
            v = f"{major}.{minor}.{pp}.{b}"
            versions.append(v)
    return versions


def scan(version_file: str, dry: bool = False):
    """主扫描逻辑。"""
    # 加载现有列表
    with open(version_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    existing = {v["version"] for v in data["versions"]}
    latest_stable = max(
        (v["version"] for v in data["versions"] if v["branch"] == "stable"),
        key=parse_version, default="1.26.33.1"
    )
    latest_preview = max(
        (v["version"] for v in data["versions"] if v["branch"] == "preview"),
        key=parse_version, default="1.26.30.29"
    )

    print(f"当前最新 stable:  {latest_stable}")
    print(f"当前最新 preview: {latest_preview}")
    print(f"已有版本数:       {len(existing)}")
    print()

    new_found = []

    for branch, start_ver in [("stable", latest_stable), ("preview", latest_preview)]:
        scan_depth = "small" if branch == "stable" else "small"
        candidates = generate_urls(start_ver, branch, scan_depth)
        print(f"[{branch}] 从 {start_ver} 向后探测 {len(candidates)} 个候选版本...")
        for i, ver in enumerate(candidates):
            if ver in existing:
                continue
            print(f"  [{i+1}/{len(candidates)}] 探测 {ver}...", end=" ", flush=True)
            if probe_head(ver, branch):
                url_fmt = STABLE_URL_FMT if branch == "stable" else PREVIEW_URL_FMT
                entry = {"version": ver, "branch": branch, "url": url_fmt.format(version=ver)}
                new_found.append(entry)
                existing.add(ver)
                print("✅ 找到！")
            else:
                print("⏭️")
            # 限速保护
            time.sleep(random.uniform(0.3, 0.8))
        print()

    if not new_found:
        print("没有发现新版本。")
        return

    print(f"发现 {len(new_found)} 个新版本：")
    for e in new_found:
        print(f"  {e['branch']:8s} {e['version']:14s}")

    if dry:
        print("\n(仅预览，未写入文件)")
        return

    # 合并 + 排序（新版本插到最前面）
    all_versions = new_found + data["versions"]
    all_versions.sort(key=lambda x: parse_version(x["version"]), reverse=True)
    data["versions"] = all_versions
    data["updated"] = time.strftime("%Y-%m-%d")

    with open(version_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n已更新 {version_file}，总计 {len(all_versions)} 个版本。")


if __name__ == "__main__":
    dry_run = "--dry" in sys.argv
    scan(VERSION_FILE, dry=dry_run)
