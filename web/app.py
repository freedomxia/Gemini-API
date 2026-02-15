"""
Gemini API Web Manager - 账号管理 + 轮询负载均衡 + OpenAI 兼容 API
"""
import asyncio
import json
import os
import time
import uuid
import hashlib
from pathlib import Path
from datetime import datetime, timedelta

from aiohttp import web
import aiohttp_cors

MAX_CONSECUTIVE_FAILURES = 3  # 连续失败次数阈值，达到后自动禁用

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
COOKIE_DIR = DATA_DIR / "cookies"
COOKIE_DIR.mkdir(exist_ok=True)
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
CONFIG_FILE = DATA_DIR / "config.json"

# ============ 数据管理 ============

def load_accounts() -> list[dict]:
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text())
    return []

def save_accounts(accounts: list[dict]):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2))

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    default = {
        "admin_password": "admin",
        "api_key": str(uuid.uuid4())[:8],
        "model": "unspecified",
        "plugin_token": uuid.uuid4().hex[:32],
        "plugin_auto_enable": True,
        "token_expiry_hours": 24,
        "max_consecutive_failures": 3,
    }
    save_config(default)
    return default

def save_config(config: dict):
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2))

# ============ 账号池 & 轮询 ============

class AccountPool:
    def __init__(self):
        self.accounts: list[dict] = load_accounts()
        self.clients: dict[str, object] = {}  # id -> GeminiClient
        self._index = 0
        self._lock = asyncio.Lock()
        self._init_locks: dict[str, bool] = {}  # 防止同一账号并发初始化

    def is_token_expired(self, account: dict) -> bool:
        """检查 token 是否已过期"""
        config = load_config()
        expiry_hours = config.get("token_expiry_hours", 24)
        if expiry_hours <= 0:
            return False  # 0 或负数表示永不过期
        updated_at = account.get("token_updated_at", "") or account.get("created_at", "")
        if not updated_at:
            return False
        try:
            updated_time = datetime.fromisoformat(updated_at)
            return datetime.now() - updated_time > timedelta(hours=expiry_hours)
        except (ValueError, TypeError):
            return False

    def get_token_remaining(self, account: dict) -> float:
        """获取 token 剩余有效时间（秒），返回 -1 表示无数据，-2 表示永不过期"""
        config = load_config()
        expiry_hours = config.get("token_expiry_hours", 24)
        if expiry_hours <= 0:
            return -2  # 永不过期
        updated_at = account.get("token_updated_at", "") or account.get("created_at", "")
        if not updated_at:
            return -1
        try:
            updated_time = datetime.fromisoformat(updated_at)
            expire_time = updated_time + timedelta(hours=expiry_hours)
            remaining = (expire_time - datetime.now()).total_seconds()
            return max(0, remaining)
        except (ValueError, TypeError):
            return -1

    async def check_expired_tokens(self):
        """后台定时检查过期 token 和自动重试异常账号"""
        while True:
            await asyncio.sleep(60)  # 每分钟检查一次
            changed = False
            for acc in self.accounts:
                if not acc.get("enabled", True):
                    continue

                # 检查过期
                if acc.get("status") == "active" and self.is_token_expired(acc):
                    config = load_config()
                    expiry_hours = config.get("token_expiry_hours", 24)
                    acc["status"] = "expired"
                    acc["error"] = f"Token 已过期（超过{expiry_hours}小时）"
                    acc["enabled"] = False
                    self.clients.pop(acc["id"], None)
                    changed = True

                # 自动重试异常账号（每 5 分钟尝试一次）
                elif acc.get("status") == "error" and acc["id"] not in self.clients:
                    last_check = acc.get("last_check", "")
                    if last_check:
                        try:
                            last_time = datetime.fromisoformat(last_check)
                            if datetime.now() - last_time < timedelta(minutes=5):
                                continue
                        except (ValueError, TypeError):
                            pass
                    asyncio.create_task(self.init_client(acc))

            if changed:
                save_accounts(self.accounts)

    async def init_client(self, account: dict):
        """初始化单个账号的 GeminiClient"""
        aid = account["id"]

        # 防止同一账号并发初始化
        if self._init_locks.get(aid):
            return
        self._init_locks[aid] = True

        try:
            await self._do_init_client(account)
        finally:
            self._init_locks.pop(aid, None)

    async def _do_init_client(self, account: dict):
        """实际执行初始化逻辑"""
        # 检查是否过期
        if self.is_token_expired(account):
            config = load_config()
            expiry_hours = config.get("token_expiry_hours", 24)
            account["status"] = "expired"
            account["error"] = f"Token 已过期（超过{expiry_hours}小时）"
            account["enabled"] = False
            self.clients.pop(account["id"], None)
            save_accounts(self.accounts)
            return

        # 先关闭旧 client，避免后台 refresh 任务泄漏和 cookie 竞争
        old_client = self.clients.pop(account["id"], None)
        if old_client:
            try:
                await old_client.close()
            except Exception:
                pass

        from gemini_webapi import GeminiClient

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            client = None
            try:
                client = GeminiClient(
                    account.get("psid", ""),
                    account.get("psidts", ""),
                    proxy=account.get("proxy") or None,
                )
                await client.init(timeout=60, auto_close=False, auto_refresh=True, refresh_interval=540)
                self.clients[account["id"]] = client
                account["status"] = "active"
                account["last_check"] = datetime.now().isoformat()
                account["error"] = ""
                account["consecutive_failures"] = 0
                save_accounts(self.accounts)
                return
            except Exception as e:
                # 失败时关闭 client，防止连接泄漏
                if client:
                    try:
                        await client.close()
                    except Exception:
                        pass
                if attempt < max_retries:
                    await asyncio.sleep(5 * attempt)  # 递增等待
                    continue
                account["status"] = "error"
                account["error"] = str(e)
                account["last_check"] = datetime.now().isoformat()
        save_accounts(self.accounts)

    async def init_all(self):
        """启动时初始化所有启用的账号"""
        for acc in self.accounts:
            if acc.get("enabled", True):
                await self.init_client(acc)

    async def get_next_client(self):
        """轮询获取下一个可用的 client"""
        async with self._lock:
            active = [(acc, self.clients[acc["id"]]) 
                      for acc in self.accounts 
                      if acc.get("enabled", True) and acc["id"] in self.clients and acc.get("status") == "active"]
            if not active:
                return None, None
            self._index = self._index % len(active)
            acc, client = active[self._index]
            self._index += 1
            return acc, client

    def add_account(self, psid: str, psidts: str, name: str = "", proxy: str = "") -> dict:
        now = datetime.now().isoformat()
        acc = {
            "id": str(uuid.uuid4())[:8],
            "name": name or f"账号{len(self.accounts)+1}",
            "psid": psid,
            "psidts": psidts,
            "proxy": proxy,
            "enabled": True,
            "status": "pending",
            "error": "",
            "created_at": now,
            "token_updated_at": now,
            "last_check": "",
            "request_count": 0,
            "consecutive_failures": 0,
        }
        self.accounts.append(acc)
        save_accounts(self.accounts)
        return acc

    def remove_account(self, account_id: str):
        old_client = self.clients.pop(account_id, None)
        if old_client:
            asyncio.create_task(self._close_client(old_client))
        self.accounts = [a for a in self.accounts if a["id"] != account_id]
        save_accounts(self.accounts)

    @staticmethod
    async def _close_client(client):
        try:
            await client.close()
        except Exception:
            pass

    def toggle_account(self, account_id: str):
        for acc in self.accounts:
            if acc["id"] == account_id:
                acc["enabled"] = not acc.get("enabled", True)
                if not acc["enabled"]:
                    old_client = self.clients.pop(account_id, None)
                    if old_client:
                        asyncio.create_task(self._close_client(old_client))
                else:
                    # 重新启用时重置失败计数和状态
                    acc["consecutive_failures"] = 0
                    if acc.get("status") in ("expired", "disabled"):
                        acc["token_updated_at"] = datetime.now().isoformat()
                        acc["status"] = "pending"
                        acc["error"] = ""
                save_accounts(self.accounts)
                return acc
        return None

    def report_failure(self, account: dict, error: str):
        """记录一次请求失败，连续失败达到阈值则自动禁用"""
        account["consecutive_failures"] = account.get("consecutive_failures", 0) + 1
        account["error"] = error
        account["last_check"] = datetime.now().isoformat()

        config = load_config()
        max_failures = config.get("max_consecutive_failures", MAX_CONSECUTIVE_FAILURES)

        if max_failures > 0 and account["consecutive_failures"] >= max_failures:
            account["status"] = "disabled"
            account["enabled"] = False
            account["error"] = f"连续失败{account['consecutive_failures']}次，已自动禁用"
            old_client = self.clients.pop(account["id"], None)
            if old_client:
                asyncio.create_task(self._close_client(old_client))
        else:
            account["status"] = "error"

        save_accounts(self.accounts)

    def report_success(self, account: dict):
        """记录一次请求成功，重置失败计数"""
        account["consecutive_failures"] = 0
        account["request_count"] = account.get("request_count", 0) + 1
        save_accounts(self.accounts)

pool = AccountPool()

# ============ 认证中间件 ============

def check_admin(request):
    config = load_config()
    auth = request.headers.get("Authorization", "")
    token = request.cookies.get("token", "")
    expected = hashlib.md5(config["admin_password"].encode()).hexdigest()
    if token != expected and auth != f"Bearer {expected}":
        raise web.HTTPUnauthorized(text="未授权")

def check_api_key(request):
    config = load_config()
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {config['api_key']}":
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Invalid API key"}), content_type="application/json")

# ============ 管理 API 路由 ============

async def api_login(request):
    data = await request.json()
    config = load_config()
    if data.get("password") == config["admin_password"]:
        token = hashlib.md5(config["admin_password"].encode()).hexdigest()
        resp = web.json_response({"ok": True, "token": token})
        resp.set_cookie("token", token, max_age=86400*7)
        return resp
    return web.json_response({"ok": False, "error": "密码错误"}, status=401)

async def api_list_accounts(request):
    check_admin(request)
    safe = []
    for acc in pool.accounts:
        a = {**acc}
        a["psid"] = a["psid"][:10] + "..." if len(a.get("psid","")) > 10 else a.get("psid","")
        a["psidts"] = a["psidts"][:10] + "..." if len(a.get("psidts","")) > 10 else a.get("psidts","")
        a["token_remaining"] = pool.get_token_remaining(acc)
        safe.append(a)
    return web.json_response(safe)

async def api_add_account(request):
    check_admin(request)
    data = await request.json()
    acc = pool.add_account(
        psid=data.get("psid", ""),
        psidts=data.get("psidts", ""),
        name=data.get("name", ""),
        proxy=data.get("proxy", ""),
    )
    asyncio.create_task(pool.init_client(acc))
    return web.json_response({"ok": True, "account": acc})

async def api_remove_account(request):
    check_admin(request)
    aid = request.match_info["id"]
    pool.remove_account(aid)
    return web.json_response({"ok": True})

async def api_toggle_account(request):
    check_admin(request)
    aid = request.match_info["id"]
    acc = pool.toggle_account(aid)
    if acc and acc.get("enabled"):
        asyncio.create_task(pool.init_client(acc))
    return web.json_response({"ok": True, "account": acc})

async def api_refresh_account(request):
    check_admin(request)
    aid = request.match_info["id"]
    for acc in pool.accounts:
        if acc["id"] == aid:
            asyncio.create_task(pool.init_client(acc))
            return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "账号不存在"}, status=404)

async def api_update_account(request):
    check_admin(request)
    aid = request.match_info["id"]
    data = await request.json()
    for acc in pool.accounts:
        if acc["id"] == aid:
            if data.get("name"):
                acc["name"] = data["name"]
            if data.get("proxy") is not None:
                acc["proxy"] = data["proxy"]
            if data.get("psid"):
                acc["psid"] = data["psid"]
            if data.get("psidts"):
                acc["psidts"] = data["psidts"]
            # 更新 token 时刷新有效期
            if data.get("psid") or data.get("psidts"):
                acc["token_updated_at"] = datetime.now().isoformat()
                acc["status"] = "pending"
                acc["error"] = ""
            save_accounts(pool.accounts)
            if data.get("psid") or data.get("psidts"):
                asyncio.create_task(pool.init_client(acc))
            return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "账号不存在"}, status=404)

async def api_get_config(request):
    check_admin(request)
    config = load_config()
    return web.json_response(config)

async def api_update_config(request):
    check_admin(request)
    data = await request.json()
    config = load_config()
    config.update(data)
    save_config(config)
    return web.json_response({"ok": True})

async def api_stats(request):
    check_admin(request)
    total = len(pool.accounts)
    active = sum(1 for a in pool.accounts if a.get("status") == "active")
    expired = sum(1 for a in pool.accounts if a.get("status") == "expired")
    disabled = sum(1 for a in pool.accounts if a.get("status") == "disabled")
    total_requests = sum(a.get("request_count", 0) for a in pool.accounts)
    return web.json_response({"total": total, "active": active, "expired": expired, "disabled": disabled, "total_requests": total_requests})

# ============ 插件 API ============

async def api_get_plugin_config(request):
    check_admin(request)
    config = load_config()
    return web.json_response({
        "success": True,
        "config": {
            "connection_url": f"/api/plugin/update-token",
            "connection_token": config.get("plugin_token", ""),
            "auto_enable_on_update": config.get("plugin_auto_enable", True),
        }
    })

async def api_save_plugin_config(request):
    check_admin(request)
    data = await request.json()
    config = load_config()
    if "connection_token" in data:
        config["plugin_token"] = data["connection_token"]
    if "auto_enable_on_update" in data:
        config["plugin_auto_enable"] = data["auto_enable_on_update"]
    save_config(config)
    return web.json_response({"ok": True})

async def api_plugin_update_token(request):
    """Chrome 扩展插件调用此接口推送 Cookie"""
    data = await request.json()
    # 验证 plugin token
    config = load_config()
    req_token = data.get("token") or request.headers.get("X-Connection-Token", "")
    if req_token != config.get("plugin_token", ""):
        return web.json_response({"success": False, "message": "Invalid connection token"}, status=401)

    psid = data.get("psid", "") or data.get("__Secure-1PSID", "")
    psidts = data.get("psidts", "") or data.get("__Secure-1PSIDTS", "")
    name = data.get("name", "") or data.get("email", "")
    proxy = data.get("proxy", "")

    if not psid:
        return web.json_response({"success": False, "message": "Missing __Secure-1PSID"}, status=400)

    # 检查是否已存在相同名称的账号，如果有则更新
    existing = None
    for acc in pool.accounts:
        if name and acc.get("name") == name:
            existing = acc
            break

    if existing:
        existing["psid"] = psid
        if psidts:
            existing["psidts"] = psidts
        if proxy:
            existing["proxy"] = proxy
        existing["token_updated_at"] = datetime.now().isoformat()
        existing["status"] = "pending"
        existing["error"] = ""
        if config.get("plugin_auto_enable", True):
            existing["enabled"] = True
        save_accounts(pool.accounts)
        asyncio.create_task(pool.init_client(existing))
        return web.json_response({"success": True, "message": "Token updated", "id": existing["id"]})
    else:
        acc = pool.add_account(psid=psid, psidts=psidts, name=name, proxy=proxy)
        asyncio.create_task(pool.init_client(acc))
        return web.json_response({"success": True, "message": "Token added", "id": acc["id"]})

# ============ OpenAI 兼容 API ============

async def openai_chat_completions(request):
    check_api_key(request)
    data = await request.json()
    messages = data.get("messages", [])
    stream = data.get("stream", False)
    model_name = data.get("model", "unspecified")

    # 提取最后一条用户消息
    prompt = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                prompt = content
            elif isinstance(content, list):
                prompt = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            break

    if not prompt:
        return web.json_response({"error": "No user message found"}, status=400)

    # 解析模型
    from gemini_webapi.constants import Model
    model = model_name
    try:
        model = Model.from_name(model_name)
    except ValueError:
        model = model_name if model_name != "unspecified" else Model.UNSPECIFIED

    # 失败时自动切换下一个账号重试（最多尝试 3 个不同账号）
    last_error = None
    tried_ids = set()
    max_account_retries = 3

    for _ in range(max_account_retries):
        acc, client = await pool.get_next_client()
        if not client or (acc and acc["id"] in tried_ids):
            # 没有更多可用账号了
            break
        tried_ids.add(acc["id"])

        try:
            if stream:
                response = web.StreamResponse()
                response.content_type = "text/event-stream"
                response.headers["Cache-Control"] = "no-cache"
                response.headers["Connection"] = "keep-alive"
                await response.prepare(request)

                resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                created = int(time.time())

                async for chunk in client.generate_content_stream(prompt, model=model):
                    delta = chunk.text_delta or ""
                    if delta:
                        sse_data = {
                            "id": resp_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                        }
                        await response.write(f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n".encode())

                end_data = {
                    "id": resp_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                await response.write(f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n".encode())
                await response.write(b"data: [DONE]\n\n")

                pool.report_success(acc)
                return response
            else:
                result = await client.generate_content(prompt, model=model)
                pool.report_success(acc)

                return web.json_response({
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": result.text},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": len(prompt), "completion_tokens": len(result.text), "total_tokens": len(prompt) + len(result.text)},
                })
        except Exception as e:
            last_error = str(e)
            pool.report_failure(acc, last_error)
            if stream:
                # 流式已经开始写了，无法重试
                return response
            # 非流式继续尝试下一个账号

    return web.json_response({"error": last_error or "No active accounts available"}, status=503)

async def openai_models(request):
    return web.json_response({
        "object": "list",
        "data": [
            {"id": "unspecified", "object": "model", "owned_by": "google"},
            {"id": "gemini-3.0-pro", "object": "model", "owned_by": "google"},
            {"id": "gemini-3.0-flash", "object": "model", "owned_by": "google"},
            {"id": "gemini-3.0-flash-thinking", "object": "model", "owned_by": "google"},
        ]
    })

# ============ 前端页面 ============

async def index(request):
    return web.FileResponse("web/static/index.html")

# ============ 启动 ============

async def on_startup(app):
    await pool.init_all()
    asyncio.create_task(pool.check_expired_tokens())

def create_app():
    app = web.Application()
    app.on_startup.append(on_startup)

    # 管理 API
    app.router.add_post("/api/login", api_login)
    app.router.add_get("/api/accounts", api_list_accounts)
    app.router.add_post("/api/accounts", api_add_account)
    app.router.add_delete("/api/accounts/{id}", api_remove_account)
    app.router.add_post("/api/accounts/{id}/toggle", api_toggle_account)
    app.router.add_post("/api/accounts/{id}/refresh", api_refresh_account)
    app.router.add_post("/api/accounts/{id}/update", api_update_account)
    app.router.add_get("/api/config", api_get_config)
    app.router.add_post("/api/config", api_update_config)
    app.router.add_get("/api/stats", api_stats)

    # 插件 API
    app.router.add_get("/api/plugin/config", api_get_plugin_config)
    app.router.add_post("/api/plugin/config", api_save_plugin_config)
    app.router.add_post("/api/plugin/update-token", api_plugin_update_token)

    # OpenAI 兼容 API
    app.router.add_post("/v1/chat/completions", openai_chat_completions)
    app.router.add_get("/v1/models", openai_models)

    # 静态文件
    app.router.add_get("/", index)
    app.router.add_static("/static", "web/static")

    # CORS
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*")
    })
    for route in list(app.router.routes()):
        cors.add(route)

    return app

if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 服务启动在 http://localhost:{port}")
    print(f"📊 管理后台: http://localhost:{port}")
    print(f"🔗 API 地址: http://localhost:{port}/v1/chat/completions")
    web.run_app(app, host="0.0.0.0", port=port)
