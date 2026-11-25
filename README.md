# BDS 版本列表

Minecraft Bedrock Dedicated Server 官方下载链接的索引仓库，
由 [BDS Manager Fluent](https://github.com/TussalZeus18028/bds_manager) 维护。

## 文件

| 文件 | 说明 |
|------|------|
| `bds_versions.json` | 版本索引（**主程序读这个**） |
| `fetch_bds_versions.py` | 采集器：抓取 + 验证 + 写入 |
| `scan_new.py` | 旧版纯嗅探脚本，已被 `fetch_bds_versions.py` 取代（保留作参考） |

## 数据格式

```json
{
  "name": "Minecraft Bedrock Dedicated Server Versions",
  "description": "BDS 版本列表，由 BDS Manager 维护",
  "updated": "2026-11-25",
  "versions": [
    {
      "version": "1.26.70.26",
      "branch": "preview",
      "url": "https://www.minecraft.net/bedrockdedicatedserver/bin-win-preview/bedrock-server-1.26.70.26.zip"
    }
  ]
}
```

- `branch`：`stable`（正式通道 `/bin-win/`）或 `preview`（预览通道 `/bin-win-preview/`）
- `versions` 按版本号降序排列
- 同一版本号只会有一个 channel（探测时 stable 优先）

## 采集器用法

```bash
# 增量采集（默认，只验证新版本）并写入
python fetch_bds_versions.py

# 只预览不写入
python fetch_bds_versions.py --dry

# 全量重新验证（校验所有历史条目是否还有效）
python fetch_bds_versions.py --full

# 指定来源
python fetch_bds_versions.py --source wiki     # 只用 wiki 抓取
python fetch_bds_versions.py --source sniff    # 只用版本号嗅探
python fetch_bds_versions.py --source both     # 默认，两者结合

# 调参
python fetch_bds_versions.py --workers 14 --timeout 8
```

## 三层采集策略

```
Layer 1  wiki 抓取      minecraft.wiki/w/Bds 版本历史表
                        一次请求拿到 300+ 候选版本号
                          ↓
Layer 2  并发 HEAD 验证  对每个候选号探测 stable / preview 两个下载路径
                        12~14 并发 + 429 指数退避
                        确认真实存在的那条并记下 channel
                          ↓
Layer 3  版本号嗅探兜底  wiki 尚未同步的新版本
                        从最新版本外扩枚举，**支持跨 minor**
```

### 为什么要跨 minor

旧版 `scan_new.py` 只在同一 minor 下枚举 patch/build，探测范围是
`1.26.{patch+1..patch+15}.{build+1..build+45}`。

但官方发大版本时 **patch 号会直接跳到 60+/70+**（例如从 `1.26.43.1`
跳到 `1.26.70.x`）。结果就是嗅探范围永远够不着，列表长期落后 ——
实测曾落后 **27 个 patch**（3 个月没更新）。

新脚本用 wiki 抓取拿到完整版本号集合，嗅探只作兜底，范围收紧到几百个候选。

### 实测性能

| 场景 | 耗时 |
|------|------|
| wiki 抓取（363 个候选） | < 3 秒 |
| 增量验证 274 个新版本 | 约 195 秒（14 并发） |

## 踩坑记录

- **wiki 需要完整请求头**：缺 `Accept-Language` / `Accept-Encoding` 会直接 **403**。
  脚本里的 `WIKI_HEADERS` 就是为此准备的，别删。
- **`lip` 输出是 GBK 不是 UTF-8**（BDS Manager 侧踩过）。
- **Minecraft 官网下载页是 React 渲染的**，HTML 里没有版本号和下载链接，
  抓不到。wiki 是可用的替代源。

## 关联

- 主程序：[TussalZeus18028/bds_manager](https://github.com/TussalZeus18028/bds_manager)
- 主程序通过 `backend/upgrade_workers.py::scrape_github_versions()` 读本仓库的
  `bds_versions.json`，并有 48 小时本地缓存。
