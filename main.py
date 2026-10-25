# -*- coding: utf-8 -*-
"""
astrbot_plugin_mt_mcp
=====================
通过 MT 管理器 MCP 服务，在聊天中读取/搜索手机文件，并把文件直接发送到聊天。

指令：
  /mt 状态                查看 MCP 连接状态与 Home 目录
  /mt 列表 [路径]         列出目录内容（默认 Home，别名 /mtls /mt ls）
  /mt 搜索 <关键词> [路径] 按文件名/内容搜索（别名 /mtfind）
  /mt 读 <路径>           读取文本文件内容（别名 /mtcat）
  /mt 发 <路径>           把文件作为聊天文件发送（别名 /mtsend /mt get）
  /mt 帮助                显示本说明

前提：
  - MT 管理器已启动 MCP 服务（MT → 侧拉栏 → 工具 → MCP 服务 → 启动）
  - AstrBot 与 MT 在同一台设备（连 127.0.0.1:8787/mcp），
    或通过 adb forward / 局域网地址可达
  - 目标路径需在 MT 的 Home 内（mcp 文件夹），或在 MT 的文件访问权限中已授权
  - 相对路径以 Home（默认 /storage/emulated/0/MT2/mcp）为起点，
    可任意进入 mcp 下的子文件夹：/mt 列表 子文件夹、/mt 读 子文件夹/文件.txt
"""

import asyncio
import base64
import json
import os
import re
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_mt_mcp"

DEFAULT_MCP_URL = "http://127.0.0.1:8787/mcp"
DEFAULT_HOME = "/storage/emulated/0/MT2/mcp"

# 列表/搜索默认每页条数
PAGE_SIZE = 50
# 文本读取默认最大字符数
TEXT_MAX_CHARS = 8000
# 二进制读取单页上限（MT 官方 64 KiB）
BYTES_PAGE = 65536
# 发送文件大小上限（超出则提示路径而不发）
SEND_MAX_BYTES = 200 * 1024 * 1024


@register(
    PLUGIN_NAME,
    "Little AL-1S",
    "通过 MT 管理器 MCP 读取/搜索手机文件，并把文件直接发送到聊天",
    "1.1.1",
)
class MtMcpPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.mcp_url = (self.config.get("mcp_url") or DEFAULT_MCP_URL).strip()
        self.mcp_token = (self.config.get("mcp_token") or "").strip()
        self.home_dir = (self.config.get("home_dir") or DEFAULT_HOME).strip().rstrip("/")
        self._req_id = 0
        self._session = None
        # 最近一次列表的缓存：{target, files:[{idx,path,name,size,type}]}
        self._last_list = None

    # ------------------------------------------------------------------
    # MCP 调用基础
    # ------------------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.mcp_token:
            h["Authorization"] = "Bearer " + self.mcp_token
        return h

    async def _rpc(self, method: str, params: dict, timeout: float = 30.0):
        """向 MT MCP 发送 JSON-RPC 请求，返回 result 字典。"""
        self._req_id += 1
        req_id = self._req_id
        body = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        session = await self._get_session()
        async with session.post(
            self.mcp_url, json=body, headers=self._headers(), timeout=timeout
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if "error" in data:
            err = data["error"]
            raise RuntimeError(err.get("message", "MCP 错误"))
        result = data.get("result", {})
        return result

    async def _call_tool(self, name: str, arguments: dict, timeout: float = 60.0):
        """调用 MCP 工具，解析返回的 text 内容为 dict。"""
        result = await self._rpc(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )
        # MT 返回 content: [{type:text, text: json字符串}]
        content = result.get("content", [])
        text = ""
        for c in content:
            if c.get("type") == "text":
                text += c.get("text", "")
        if not text.strip():
            return {"ok": False, "data": None, "error": {"message": "无返回内容"}}
        try:
            payload = json.loads(text)
        except Exception:
            return {"ok": True, "data": {"raw": text}, "error": None}
        if not payload.get("ok"):
            err = payload.get("error") or {}
            raise RuntimeError(err.get("message", "工具执行失败"))
        return payload

    # ------------------------------------------------------------------
    # 路径辅助
    # ------------------------------------------------------------------
    def _norm_path(self, path: str) -> str:
        """解析用户输入的路径为完整绝对路径。

        - 空 / . / ./ / → 返回 Home
        - 相对路径（不以 / 开头）→ 拼到 Home 下（Home 即 mcp 文件夹，
          可任意进入 mcp 下的子文件夹，如 /mt 列表 子文件夹）
        - 绝对路径 → 原样返回（需 MT 已授权该目录）
        """
        p = (path or "").strip().rstrip("/")
        if not p or p in (".", "./", "/"):
            return self.home_dir or DEFAULT_HOME
        if p.startswith("/"):
            return p
        home = self.home_dir or DEFAULT_HOME
        return home + "/" + p

    @staticmethod
    def _parse_listing(listing) -> list:
        """解析 mt_file_list 返回的 listing 行。"""
        items = []
        for line in listing:
            # 形如: "f 2199184 2026-10-25T07:51:56+08:00 文件名"
            #        "d 2026-10-25T07:51:56+08:00 目录名"
            m = re.match(r"^(f|d)\s+(\d+)?\s*([0-9TZ:+\-]+)\s+(.+)$", line)
            if not m:
                items.append({"type": "?", "name": line, "size": None, "mtime": ""})
                continue
            typ = "dir" if m.group(1) == "d" else "file"
            size = int(m.group(2)) if m.group(2) else None
            items.append({
                "type": typ,
                "name": m.group(4),
                "size": size,
                "mtime": m.group(3),
            })
        return items

    @staticmethod
    def _human_size(n) -> str:
        if n is None:
            return "-"
        f = float(n)
        for unit in ("B", "KB", "MB", "GB"):
            if f < 1024.0 or unit == "GB":
                return ("%.1f%s" % (f, unit)) if unit != "B" else ("%dB" % int(n))
            f /= 1024.0
        return "%dB" % int(n)

    # ------------------------------------------------------------------
    # 指令：状态
    # ------------------------------------------------------------------
    @filter.command("mt 状态", alias={"MT状态", "mt状态"})
    async def mt_status(self, event: AstrMessageEvent):
        """查看 MCP 连接状态与 Home 目录。"""
        lines = [
            "📱 MT MCP 状态",
            "———————————",
            "地址：%s" % self.mcp_url,
            "Home：%s" % (self.home_dir or DEFAULT_HOME),
            "认证：%s" % ("已启用(Bearer)" if self.mcp_token else "未启用"),
        ]
        try:
            r = await self._rpc("initialize", {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": PLUGIN_NAME, "version": "1.1.1"},
            })
            si = r.get("serverInfo", {})
            lines.append("连接：✅ %s %s" % (si.get("name", "MT MCP"), si.get("version", "")))
        except Exception as e:
            lines.append("连接：❌ %s" % e)
        # 探测 Home
        try:
            st = await self._call_tool("mt_file_stat", {"path": self.home_dir or DEFAULT_HOME})
            data = st.get("data") or {}
            lines.append("Home 权限：%s" % (data.get("access", "未知")))
        except Exception as e:
            lines.append("Home 权限：❌ %s" % e)
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 指令：列表
    # ------------------------------------------------------------------
    @filter.command("mt 列表", alias={"mtls", "MT列表", "mt ls"})
    async def mt_list(self, event: AstrMessageEvent):
        """列出目录内容。用法：/mt 列表 [路径]"""
        raw = event.message_str.strip()
        body = raw[1:] if raw.startswith("/") else raw
        arg = self._strip_cmd(body, ("mt 列表", "mtls", "MT列表", "mt ls"))
        path = self._norm_path(arg)
        target = path or self.home_dir or DEFAULT_HOME
        try:
            payload = await self._call_tool("mt_file_list", {
                "path": target,
                "recursive": False,
                "cursor": "",
                "limit": PAGE_SIZE,
            })
        except Exception as e:
            yield event.plain_result("❌ 列目录失败：%s" % e)
            return
        data = payload.get("data") or {}
        items = self._parse_listing(data.get("listing", []))
        pag = data.get("pagination", {})
        if not items:
            yield event.plain_result("📁 %s\n（空目录）" % target)
            return
        # 生成带序号的缓存：仅文件参与编号（目录不参与下载）
        files = []
        for it in items:
            if it["type"] == "dir":
                continue
            files.append({
                "path": target.rstrip("/") + "/" + it["name"],
                "name": it["name"],
                "size": it["size"],
            })
        self._last_list = {"target": target, "files": files}

        lines = ["📁 %s（%d 项，%d 个文件）" % (target, len(items), len(files)), "———————————"]
        idx = 0
        for it in items:
            if it["type"] == "dir":
                lines.append("📂 %s/" % it["name"])
            else:
                idx += 1
                lines.append("%d. 📄 %s  %s" % (idx, it["name"], self._human_size(it["size"])))
        if pag.get("hasMore"):
            lines.append("……（还有更多，可指定更具体的子目录）")
        lines.append("———————————")
        lines.append("发送 /mt 发 <序号> 下载对应文件，如 /mt 发 1")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 指令：搜索
    # ------------------------------------------------------------------
    @filter.command("mt 搜索", alias={"mtfind", "MT搜索", "mt find"})
    async def mt_search(self, event: AstrMessageEvent):
        """按文件名/内容搜索。用法：/mt 搜索 <关键词> [路径]"""
        raw = event.message_str.strip()
        body = raw[1:] if raw.startswith("/") else raw
        arg = self._strip_cmd(body, ("mt 搜索", "mtfind", "MT搜索", "mt find"))
        if not arg:
            yield event.plain_result("用法：/mt 搜索 <关键词> [路径]\n例如：/mt 搜索 config /storage/emulated/0/Download")
            return
        # 拆分关键词与路径：支持绝对路径与相对路径（相对路径以 Home 为起点）
        keyword = arg
        path = ""
        parts = arg.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].strip():
            # 最后一个词不含空格且原参数确实有空隙 → 视为路径
            if parts[1].startswith("/") or parts[1].startswith(".") or "/" in parts[1]:
                keyword, path = parts[0].strip(), parts[1].strip()
        target = self._norm_path(path)
        try:
            payload = await self._call_tool("mt_file_search", {
                "path": target,
                "recursive": True,
                "namePattern": keyword,
                "namePatternType": "literal",
                "nameMatchMode": "contains",
                "nameCaseSensitive": False,
                "minSize": -1,
                "maxSize": -1,
                "minModifiedAt": "",
                "maxModifiedAt": "",
                "contentPattern": "",
                "contentPatternType": "literal",
                "contentCaseSensitive": False,
                "cursor": "",
                "limit": PAGE_SIZE,
            }, timeout=90.0)
        except Exception as e:
            yield event.plain_result("❌ 搜索失败：%s" % e)
            return
        data = payload.get("data") or {}
        results = data.get("results") or data.get("matches") or data.get("items") or []
        pag = data.get("pagination", {})
        if not results:
            yield event.plain_result("🔍 未找到名称包含「%s」的文件（%s）" % (keyword, target))
            return
        lines = ["🔍 「%s」搜索到 %d 个结果：" % (keyword, len(results)), "———————————"]
        for i, r in enumerate(results, 1):
            # 结果可能是字符串或 dict
            if isinstance(r, dict):
                p = r.get("path") or r.get("name") or ""
                typ = r.get("type", "")
                sz = r.get("size")
                lines.append("%d. %s%s  %s" % (i, "[目录] " if typ == "directory" else "", p, self._human_size(sz)))
            else:
                lines.append("%d. %s" % (i, r))
        if pag.get("hasMore"):
            lines.append("……（还有更多）")
        lines.append("———————————")
        lines.append("发送 /mt 读 <完整路径> 查看内容，/mt 发 <完整路径> 发送文件")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 指令：读文本
    # ------------------------------------------------------------------
    @filter.command("mt 读", alias={"mtcat", "MT读", "mt cat"})
    async def mt_read(self, event: AstrMessageEvent):
        """读取文本文件内容。用法：/mt 读 <路径>"""
        raw = event.message_str.strip()
        body = raw[1:] if raw.startswith("/") else raw
        arg = self._strip_cmd(body, ("mt 读", "mtcat", "MT读", "mt cat"))
        if not arg:
            yield event.plain_result("用法：/mt 读 <路径>\n例如：/mt 读 /storage/emulated/0/MT2/mcp/test.txt")
            return
        path = arg.strip()
        try:
            payload = await self._call_tool("mt_file_read_text", {
                "path": path,
                "encoding": "utf-8",
                "targetVersion": "",
                "limit": 500,
                "maxChars": TEXT_MAX_CHARS,
                "startLine": 0,
                "startColumn": 0,
            }, timeout=60.0)
        except Exception as e:
            yield event.plain_result("❌ 读取失败：%s" % e)
            return
        data = payload.get("data") or {}
        text = data.get("text") or data.get("content") or ""
        if not text:
            yield event.plain_result("📄 %s\n（空文件或无文本内容）" % path)
            return
        # 过长时截断
        shown = text
        truncated = False
        if len(shown) > TEXT_MAX_CHARS:
            shown = shown[:TEXT_MAX_CHARS]
            truncated = True
        yield event.plain_result("📄 %s\n———————————\n%s%s" % (
            path, shown, "\n……（内容过长已截断）" if truncated else ""
        ))

    # ------------------------------------------------------------------
    # 指令：发送文件
    # ------------------------------------------------------------------
    @filter.command("mt 发", alias={"mtsend", "MT发", "mt get"})
    async def mt_send(self, event: AstrMessageEvent):
        """把文件作为聊天文件发送。用法：/mt 发 <序号> 或 /mt 发 <路径>"""
        raw = event.message_str.strip()
        body = raw[1:] if raw.startswith("/") else raw
        arg = self._strip_cmd(body, ("mt 发", "mtsend", "MT发", "mt get"))
        if not arg:
            yield event.plain_result("用法：/mt 发 <序号> 或 /mt 发 <路径>\n先用 /mt 列表 查看序号，如 /mt 发 1")
            return
        arg = arg.strip()

        # 纯数字 → 按上次列表序号取文件
        if arg.isdigit():
            if not self._last_list or not self._last_list.get("files"):
                yield event.plain_result("⚠️ 请先发送 /mt 列表 获取文件序号。")
                return
            n = int(arg)
            files = self._last_list["files"]
            if n < 1 or n > len(files):
                yield event.plain_result("❌ 序号超出范围（1-%d）。请重新发送 /mt 列表 确认。" % len(files))
                return
            path = files[n - 1]["path"]
            async for result in self._send_file(event, path):
                yield result
            return

        path = arg
        async for result in self._send_file(event, path):
            yield result

    async def _send_file(self, event: AstrMessageEvent, path: str):
        """发送指定路径的文件（内部方法）。"""
        # 先 stat 看类型与大小
        try:
            st = await self._call_tool("mt_file_stat", {"path": path})
        except Exception as e:
            yield event.plain_result("❌ 无法访问文件：%s" % e)
            return
        st_data = st.get("data") or {}
        if st_data.get("type") == "directory":
            yield event.plain_result("❌ %s 是目录，不能作为文件发送。" % path)
            return
        size = st_data.get("size")
        if size and size > SEND_MAX_BYTES:
            yield event.plain_result(
                "⚠️ 文件过大（%s），超过发送上限，请直接用 MT 查看。\n路径：%s"
                % (self._human_size(size), path)
            )
            return

        yield event.plain_result("⏳ 正在读取并发送：%s ..." % path)

        # 二进制分页读取，写本地缓存
        cache_dir = self._cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        name = os.path.basename(path) or "mt_file"
        cache_path = os.path.join(cache_dir, name)
        offset = 0
        target_version = ""
        total = None
        try:
            with open(cache_path, "wb") as f:
                while True:
                    payload = await self._call_tool("mt_file_read_bytes", {
                        "path": path,
                        "targetVersion": target_version,
                        "byteOffset": offset,
                        "maxBytes": BYTES_PAGE,
                    }, timeout=60.0)
                    data = payload.get("data") or {}
                    hex_str = data.get("hex") or ""
                    if not hex_str:
                        break
                    try:
                        chunk = bytes.fromhex(hex_str)
                    except Exception:
                        yield event.plain_result("❌ 文件数据解析失败（hex 格式错误）。")
                        return
                    if not chunk:
                        break
                    f.write(chunk)
                    # 使用 MT 返回的 nextByteOffset 作为下一页偏移
                    offset = data.get("nextByteOffset", offset + len(chunk))
                    target_version = data.get("targetVersion") or target_version
                    total = data.get("totalBytes")
                    # 提前结束条件
                    if data.get("hasMore") is False:
                        break
                    if total is not None and offset >= total:
                        break
                    if len(chunk) < BYTES_PAGE:
                        break
        except Exception as e:
            yield event.plain_result("❌ 读取文件失败：%s" % e)
            return

        if offset == 0:
            yield event.plain_result("⚠️ 未能读取到文件内容：%s" % path)
            return

        # 发送文件
        try:
            from astrbot.api.message_components import File
            yield event.chain_result([File(name=name, file="file:///" + cache_path.lstrip("/"))])
        except ImportError:
            try:
                from astrbot.core.message.components import File
                yield event.chain_result([File(name=name, file="file:///" + cache_path.lstrip("/"))])
            except Exception as e:
                logger.error("[mt_mcp] 发送文件失败: %s", e)
                yield event.plain_result("当前适配器不支持发送文件组件。\n文件已缓存：%s" % cache_path)
            return
        except Exception as e:
            logger.error("[mt_mcp] 发送文件失败: %s", e)
            yield event.plain_result("发送文件失败：%s\n文件已缓存：%s" % (e, cache_path))

    # ------------------------------------------------------------------
    # 指令：帮助
    # ------------------------------------------------------------------
    @filter.command("mt 帮助", alias={"MT帮助", "mt help"})
    async def mt_help(self, event: AstrMessageEvent):
        """显示使用说明。"""
        yield event.plain_result(
            "📖 MT MCP 文件助手使用说明\n"
            "———————————\n"
            "【前提】MT 已启动 MCP 服务；AstrBot 与 MT 同机或网络可达；目标路径已在 MT 授权\n"
            "/mt 状态            查看连接状态与 Home\n"
            "/mt 列表 [路径]     列出目录，文件带序号（默认 Home）\n"
            "/mt 搜索 <关键词> [路径]  按文件名搜索\n"
            "/mt 读 <路径>       读取文本文件内容\n"
            "/mt 发 <序号>       发送上次列表中对应序号的文件\n"
            "/mt 发 <路径>       按路径发送文件\n"
            "/mt 帮助            显示本说明\n"
            "———————————\n"
            "序号用法：\n"
            "  1. /mt 列表        → 看到带序号的列表\n"
            "  2. /mt 发 1        → 直接发送第 1 个文件\n"
            "———————————\n"
            "路径规则：\n"
            "· 相对路径以 Home（mcp 文件夹）为起点：\n"
            "  /mt 列表 子文件夹\n"
            "  /mt 读 子文件夹/文件.txt\n"
            "· 绝对路径需 MT 已授权该目录：\n"
            "  /mt 列表 /storage/emulated/0/Download"
        )

    # ------------------------------------------------------------------
    # 内部工具
    # -----------------------------------------------------------------------------
    def _cache_dir(self) -> str:
        """本地缓存目录（AstrBot data 目录下）。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            base = get_astrbot_data_path()
        except Exception:
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        d = os.path.join(str(base), "plugin_data", PLUGIN_NAME, "cache")
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _strip_cmd(body: str, cmds) -> str:
        """从消息体去掉指令前缀，返回剩余参数。"""
        for c in cmds:
            if body == c:
                return ""
            if body.startswith(c + " "):
                return body[len(c) + 1:].strip()
        # 兜底：去掉第一个空白词
        parts = body.split(" ", 1)
        return parts[1].strip() if len(parts) == 2 else ""

    async def terminate(self):
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        logger.info("[mt_mcp] 插件已卸载。")
