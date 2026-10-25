# astrbot_plugin_mt_mcp · MT MCP 文件助手

AstrBot v4.x 插件：通过 **MT 管理器 MCP 服务**，在聊天中读取、搜索手机文件，并把文件**直接发送到聊天**。

## 功能

| 指令 | 说明 |
|---|---|
| `/mt 状态` | 查看 MCP 连接状态、Home 目录、认证状态 |
| `/mt 列表 [路径]`（别名 `/mtls`） | 列出目录内容，**文件带序号**；支持相对路径（`/mt 列表 子文件夹`） |
| `/mt 搜索 <关键词> [路径]`（别名 `/mtfind`） | 按文件名搜索（含子目录），路径支持相对/绝对 |
| `/mt 读 <路径>`（别名 `/mtcat`） | 读取文本文件内容（UTF-8，上限 8000 字符），支持相对路径 |
| `/mt 发 <序号>`（别名 `/mtsend`） | **按上次列表的序号直接下载发送文件**，如 `/mt 发 1` |
| `/mt 发 <路径>` | 按路径发送文件，支持相对/绝对路径 |
| `/mt 帮助` | 显示使用说明 |

## 安装前提

1. **MT 管理器** 已启动 MCP 服务：MT 主界面 → 侧拉栏 → 工具 → **MCP 服务** → **启动**
2. **AstrBot 与 MT 在同一台设备**（连 `http://127.0.0.1:8787/mcp`），或通过 `adb forward` / 局域网地址让 AstrBot 可达手机
3. 目标文件需在 **MT 的 Home 目录**（默认 `/storage/emulated/0/MT2/mcp`）内，或在 **MT 的文件访问权限**（MCP 服务 → 设置 → 文件）中已授权该路径

## 配置项（AstrBot 管理面板 → 插件 → MT MCP 文件助手 → 配置）

| 配置 | 默认值 | 说明 |
|---|---|---|
| `mcp_url` | `http://127.0.0.1:8787/mcp` | MT MCP 服务地址（从 MT 中复制的完整地址，含 `/mcp`） |
| `mcp_token` | 空 | 若 MT 开启了 Bearer 令牌认证，填写完整令牌（不带 `Bearer ` 前缀） |
| `home_dir` | `/storage/emulated/0/MT2/mcp` | MT 的 Home 目录，相对路径以此为起点 |

## 使用示例

```
/mt 状态
📱 MT MCP 状态
———————————
地址：http://127.0.0.1:8787/mcp
Home：/storage/emulated/0/MT2/mcp
认证：未启用
连接：✅ MT MCP 0.2.0
Home 权限：read_write
```
/mt 列表
📁 /storage/emulated/0/MT2/mcp（2 项，2 个文件）
———————————
1. 📄 点歌姬②_1.0.1.apk  2.1MB
2. 📄 点歌姬②_1.0.apk    2.1MB
———————————
发送 /mt 发 <序号> 下载对应文件，如 /mt 发 1

# 按序号下载
/mt 发 1
⏳ 正在读取并发送：/storage/emulated/0/MT2/mcp/点歌姬②_1.0.1.apk ...
[文件消息] 点歌姬②_1.0.1.apk

# 进入 mcp 下的子文件夹（相对路径）
/mt 列表 子文件夹
📁 /storage/emulated/0/MT2/mcp/子文件夹（3 项，2 个文件）
———————————
📂 内层/
1. 📄 readme.txt  1.2KB
2. 📄 logo.png    45.3KB
———————————
发送 /mt 发 <序号> 下载对应文件，如 /mt 发 1

/mt 发 2
⏳ 正在读取并发送：/storage/emulated/0/MT2/mcp/子文件夹/logo.png ...
[文件消息] logo.png
```

## 实现细节

- **MCP 调用**：插件直接实现 JSON-RPC over HTTP（`initialize` + `tools/call`），不依赖 MCP Python SDK，减少安装负担
- **工具对接**（参数已按 MT MCP 0.2.0 实测校准）：
  - `mt_file_list`：列目录（`path` / `recursive` / `cursor` / `limit`）
  - `mt_file_search`：按文件名/内容搜索（`namePattern` 等）
  - `mt_file_read_text`：读取文本（`encoding` / `maxChars` / `startLine` 等）
  - `mt_file_read_bytes`：二进制分页读取（`byteOffset` / `maxBytes`，单页 64 KiB）
  - `mt_file_stat`：获取类型/大小/访问权限
- **发送文件**：分页读取二进制 → base64 解码 → 写入 AstrBot 本地缓存 → 用 `File` 消息组件发送（`file://` 本地路径，要求协议端如 NapCat 与 AstrBot 同机）
- **安全**：文件访问受 MT 的权限规则约束（Home 内可读写、其余路径需在 MT 中授权）；插件不自行扩大权限
- **超限保护**：文本读取上限 8000 字符；发送文件上限 200MB，超出则只返回路径提示

## 依赖

- AstrBot >= 4.16（Star 架构）
- aiohttp（AstrBot 内置）

## 已知限制

- 文件发送走 `file://` 本地路径，**要求协议端（NapCat 等）与 AstrBot 同机部署**
- MT MCP 默认无认证、仅本机回环；若通过局域网暴露，建议在 MT 中开启 Bearer 令牌认证
- 搜索功能默认只按文件名匹配（`nameMatchMode=contains`）；内容搜索可后续扩展

## 作者

Little AL-1S
