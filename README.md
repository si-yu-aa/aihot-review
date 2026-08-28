# AI HOT Review

一个零依赖、本地优先的 AI 资讯审阅台。它从
[AI HOT](https://aihot.virxact.com) 的公开接口拉取最近动态，按产业信号打分，
然后提供浏览、搜索、标记 `keep / review / drop`、强度判断和备注功能。

> 这个仓库是“本地审阅与打标客户端”，不是 `aihot.virxact.com` 的后端源码。
> 上游服务不可用时，本地历史数据和已有打标仍可读取。

## 特性

- 只依赖 Python 标准库，无需 Node.js、数据库或 API Key。
- 保存所有拉取条目；分数只用于排序，不会静默丢弃低分信息。
- 支持搜索、分类、决策、强度、已读/未读和快捷键审阅。
- JSON/JSONL 持久化，原始拉取批次和用户决策分开保存。
- 缓存合并后的 inbox；打标和已读更新不会重扫全部历史文件。
- 可选接入 Miner 的 `state/tree.json`，在界面中选择研究节点。
- 默认只监听 `127.0.0.1`，并提供 Docker、launchd 和 CI 示例。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/si-yu-aa/aihot-review.git
cd aihot-review
python3 server.py --pull --hours 24
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

如果第一次只想启动界面、不立即访问上游：

```bash
python3 server.py
```

随后点击页面右上角的 `Pull Latest`。

## 数据位置

独立克隆运行时，数据默认写入仓库下的 `data/`；该目录内容已被 Git 忽略。

```text
data/
├── aihot-runs/               每次拉取的完整快照
├── aihot-latest.json         最新一次拉取
├── aihot-decisions.jsonl     append-only 用户决策
└── aihot-views.jsonl         append-only 已读记录
```

当程序仍位于 Miner 的 `apps/aihot-review/` 中时，它会自动识别 Miner 根目录，
继续使用 `state/signal_inbox/` 和 `state/tree.json`，因此原有数据无需迁移。

也可以显式指定：

```bash
python3 server.py \
  --data-dir /absolute/path/to/aihot-data \
  --tree-path /absolute/path/to/miner/state/tree.json \
  --pull --hours 24
```

## 配置

命令行参数优先用于单次启动；环境变量适合 launchd、Docker 等长期运行环境。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AIHOT_DATA_DIR` | 独立运行时为 `./data` | 持久化数据目录 |
| `AIHOT_TREE_PATH` | 自动发现或留空 | 可选 Miner `tree.json` 路径 |
| `AIHOT_ITEMS_URL` | AI HOT 公开 items 接口 | 自定义或测试上游地址 |
| `AIHOT_USER_AGENT` | 内置浏览器 UA | 覆盖访问公开接口时的 User-Agent |

查看全部参数：

```bash
python3 server.py --help
```

## 审阅流程

- `j` / `k`：向下 / 向上选择。
- `1` / `2` / `3`：设置 `keep` / `review` / `drop` 并自动保存。
- `h` / `m` / `l`：设置 high / medium / low 强度并自动保存。
- `o`：打开原始来源。
- `s`：立即保存当前条目。

点击或键盘选中条目约 0.8 秒后会记为已读。保存过决策的条目也会被视为已读。
筛选本身不会改变已读状态。

## 本地 API

服务默认仅供本机 UI 使用：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康、版本和本地数据计数 |
| `GET` | `/api/signals` | 分页查询 inbox |
| `GET` | `/api/nodes` | 读取可选 Miner 节点 |
| `POST` | `/api/pull` | 拉取新的 AI HOT 条目 |
| `POST` | `/api/decision` | 追加一条审阅决策 |
| `POST` | `/api/view` | 追加一条已读记录 |

健康检查：

```bash
curl -fsS http://127.0.0.1:8765/api/health
```

## Docker

```bash
docker compose up --build
```

Compose 只把端口发布到本机回环地址，并使用名为 `aihot-data` 的 volume 保存数据。

## macOS 后台运行

参考 [`deploy/com.example.aihot-review.plist`](deploy/com.example.aihot-review.plist)。
把其中的 `REPLACE_WITH_*` 替换为本机绝对路径后，再加载 LaunchAgent。详细步骤见
[`docs/macos-launchd.md`](docs/macos-launchd.md)。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py
node --check app.js  # 可选；只做前端语法检查
```

项目 CI 会在 Python 3.10、3.12 和 3.13 上运行单元测试，并检查 Python / JavaScript
语法。

## 安全边界

本服务没有账号体系，也没有面向公网的鉴权层。请保持默认的 `127.0.0.1` 监听；
若要给多用户或公网访问，应先放在带认证、TLS 和访问控制的反向代理之后。

打标记录和拉取快照可能反映个人研究偏好，因此 `data/`、日志、临时文件和 `.env`
都不会被 Git 跟踪。提交前可运行：

```bash
git status --short
git grep -nEi 'token|secret|password|api[_-]?key'
```

## 许可证

[MIT](LICENSE)
