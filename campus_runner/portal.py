"""User-operated school login; ephemeral browser and read-only instance import."""
import copy
import hashlib
import ipaddress
import json
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

ORIGIN = "https://ailab.snnu.edu.cn"
URL = ORIGIN + "/user/instance"
LINK_PATH = "M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7"


def endpoint(text):
    text = text.strip()
    match = re.fullmatch(r"(?:\[([^]]+)\]|([^\s/:]+)):(\d{1,5})", text)
    if not match:
        raise ValueError("平台外部地址不是有效的主机:端口")
    host, port = match[1] or match[2], int(match[3])
    if not 1 <= port <= 65535 or host in ("*", "0.0.0.0", "::"):
        raise ValueError("平台尚未分配可连接的 SSH 地址")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError("平台返回的主机名称无效")
    return {"host": host, "port": port}


def parse_connection(text):
    """Only accept the explicitly labelled TCP/22 mapping, never other ports."""
    if not re.search(r"(?:类型|Type)\s*[:：]?\s*tcp\b", text, re.I):
        return []
    if not re.search(r"(?:实例内部地址|实例地址|Instance\s*address)\s*[:：]?\s*(?:\*|[^\s]+):22(?!\d)", text, re.I):
        return []
    match = re.search(r"(?:服务外部地址|服务地址|Service\s*address)\s*[:：]?\s*([\s\S]+)", text, re.I)
    if not match:
        return []
    results = []
    for token in re.findall(r"\[[0-9a-fA-F:]+\]:\d+|[A-Za-z0-9.-]+:\d+", match[1]):
        value = endpoint(token)
        if value not in results:
            results.append(value)
    return results


def expiry(text, observed, exact=None):
    if isinstance(exact, (int, float)) and not isinstance(exact, bool) and 0 < exact < 32503680000:
        return {"expires_at": datetime.fromtimestamp(exact, timezone.utc).isoformat(), "expiry_estimated": False}
    # Round down by one displayed unit to avoid overstating remaining lifetime.
    parts = re.findall(r"(\d+)\s*(天|小时|分钟|分|秒)", text)
    if parts and not any(word in text for word in ("超过", "大于", "以上", "过期")):
        units = {"天": 86400, "小时": 3600, "分钟": 60, "分": 60, "秒": 1}
        seconds = max(0, sum(int(n) * units[u] for n, u in parts) - min(units[u] for _, u in parts))
        return {"expires_at": (observed + timedelta(seconds=seconds)).isoformat(), "expiry_estimated": True}
    return {"expires_at": None, "expiry_estimated": True}


class Observations:
    """Keep only whitelisted instance fields from normal page traffic, no auth data."""
    def __init__(self):
        self.apps = {}

    def receive(self, payload):
        if not isinstance(payload, (str, bytes)) or len(payload) > 4_000_000:
            return
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError, RecursionError):
            return
        stack = [(value, 0)]
        budget = 20000
        while stack and budget:
            item, depth = stack.pop()
            budget -= 1
            if depth > 20:
                continue
            if isinstance(item, dict):
                if isinstance(item.get("eid"), (int, str)) and "expStopAt" in item and "name" in item:
                    if len(self.apps) >= 2000 and str(item["eid"]) not in self.apps:
                        continue
                    self.apps[str(item["eid"])] = {k: item.get(k) for k in ("eid", "name", "expStopAt")}
                stack.extend((v, depth + 1) for v in item.values() if isinstance(v, (dict, list)))
            elif isinstance(item, list):
                stack.extend((v, depth + 1) for v in item if isinstance(v, (dict, list)))


class SchoolBrowser:
    def __init__(self):
        self.observations = Observations()

    def open(self):
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(channel="msedge", headless=False)
            self.context = self.browser.new_context(locale="zh-CN", accept_downloads=False)
            self.page = self.context.new_page()
            self.page.on("response", self.response)
            self.page.on("websocket", self.websocket)
            self.page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            self.close()
            raise ValueError("学校页面未能打开。请检查校园网络及 Microsoft Edge 安装，然后重试。") from None

    def response(self, response):
        if urlsplit(response.url).netloc == urlsplit(ORIGIN).netloc and "json" in response.headers.get("content-type", ""):
            try:
                self.observations.receive(response.body())
            except Exception:
                pass

    def websocket(self, socket):
        if urlsplit(socket.url).netloc == urlsplit(ORIGIN).netloc:
            socket.on("framereceived", self.observations.receive)

    def tick(self):
        if self.page.is_closed():
            raise ValueError("登录窗口已关闭，请重新打开导入窗口")
        self.page.wait_for_timeout(100)

    def scan(self):
        current = urlsplit(self.page.url)
        if (current.scheme, current.netloc, current.path.rstrip('/')) != ('https', urlsplit(ORIGIN).netloc, '/user/instance') or not self.page.locator('[data-cy="instanceView"]').count():
            raise ValueError("请先完成登录，并进入学校平台的实例列表页")
        observed = datetime.now(timezone.utc)
        rows = self.page.locator('[data-cy="instanceView"] tr').evaluate_all("""rows => rows.map(row => ({
            links: [...row.querySelectorAll('a[href]')].map(a => ({href:a.getAttribute('href'), name:a.textContent.trim()})),
            cells: [...row.querySelectorAll('td')].map(td => td.innerText.trim())
        }))""")
        result = {}
        for row in rows:
            for link in row["links"]:
                match = re.fullmatch(r"/user/instance/([A-Za-z0-9_-]+)(?:/node/[A-Za-z0-9_-]+)?/?", link["href"])
                if not match:
                    continue
                eid = match[1]
                # Remaining time is the cell containing duration text; never parse all row numbers.
                remaining = next((c for c in row["cells"] if re.fullmatch(r"[\d\s天小时分钟秒]+(?:后到期|到期)?", c)), "")
                record = self.observations.apps.get(eid, {})
                result[eid] = {"platform_id": eid, "name": link["name"], "href": link["href"],
                               "state": (row["cells"][0] if row["cells"] else "") or "请在学校窗口核对", "remaining_text": remaining,
                               "observed_at": observed.isoformat(), **expiry(remaining, observed, record.get("expStopAt"))}
        return list(result.values())

    def connections(self, candidate):
        if self.page.get_by_role("dialog").count():
            self.page.keyboard.press("Escape")
        link = self.page.locator(f'a[href="{candidate["href"]}"]')
        if link.count() != 1:
            raise ValueError("实例列表已变化，请重新读取列表")
        row = link.locator("xpath=ancestor::tr")
        icons = row.locator(f'svg:has(path[d^="{LINK_PATH}"])')
        if icons.count() != 1:
            raise ValueError("程序无法唯一识别连接按钮。请在学校窗口打开该实例的连接弹窗，再点击读取当前弹窗。")
        icons.click(timeout=5000)
        return self.dialog()

    def dialog(self):
        dialog = self.page.get_by_role("dialog")
        dialog.wait_for(state="visible", timeout=5000)
        # Each chip is one service in the platform's information dialog.
        chips = dialog.locator('.MuiChip-clickable')
        results = []
        count = chips.count()
        if count > 32:
            raise ValueError("连接弹窗服务数量异常，请手动选择 SSH 服务")
        for index in range(max(1, count)):
            if count:
                chips.nth(index).click()
            for mapping in parse_connection(dialog.inner_text()):
                if mapping not in results:
                    results.append(mapping)
        if not results:
            raise ValueError("弹窗未提供 TCP 内部端口 22 的有效外部映射")
        return results

    def close(self):
        try:
            if hasattr(self, "browser"):
                self.browser.close()
        finally:
            if hasattr(self, "playwright"):
                self.playwright.stop()


class Portal:
    """One bounded, in-memory login session. All Playwright calls stay on one thread."""
    def __init__(self, factory=SchoolBrowser):
        self.factory = factory
        self.lock = threading.RLock()
        self.commands = queue.Queue(maxsize=2)
        self.thread = None
        self.state = {"status": "closed", "candidates": []}
        self.stopping = threading.Event()

    def status(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def update(self, **changes):
        with self.lock:
            self.state.update(changes)

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.status()
            self.commands = queue.Queue(maxsize=2)
            self.stopping.clear()
            self.state = {"session_id": uuid.uuid4().hex, "status": "opening", "candidates": [], "message": "正在打开学校登录窗口"}
            self.thread = threading.Thread(target=self.work, daemon=True, name="school-import")
            self.thread.start()
            return self.status()

    def command(self, action, session_id, platform_id=None):
        with self.lock:
            if session_id != self.state.get("session_id") or self.state["status"] not in ("waiting", "ready"):
                raise ValueError("导入会话已失效或正在处理，请稍后重新打开")
            if action not in ("scan", "connections", "dialog"):
                raise ValueError("未知导入操作")
            if action != "scan" and not any(c["platform_id"] == platform_id for c in self.state["candidates"]):
                raise ValueError("请先选择本次读取到的实例")
            self.commands.put_nowait((action, platform_id))
            self.state.update(status="busy", message="程序正在读取学校页面")
        return self.status()

    def work(self):
        browser = self.factory()
        try:
            browser.open()
            self.update(status="waiting", message="请在学校窗口登录，然后点击读取实例列表")
            deadline = time.monotonic() + 1800
            while not self.stopping.is_set() and time.monotonic() < deadline:
                browser.tick()
                try:
                    action, eid = self.commands.get_nowait()
                except queue.Empty:
                    continue
                try:
                    if action == "scan":
                        candidates = browser.scan()
                        self.update(candidates=candidates, selected=None, mappings=[], message="已读取当前页；其他分页请在学校窗口切换后重新读取")
                    else:
                        candidate = next(c for c in self.status()["candidates"] if c["platform_id"] == eid)
                        mappings = browser.connections(candidate) if action == "connections" else browser.dialog()
                        self.update(selected=eid, mappings=mappings, message="请核对所选实例的连接映射，再填写本地配置")
                    self.update(status="ready")
                except Exception as exc:
                    message = str(exc) if isinstance(exc, ValueError) else "页面读取失败，请检查登录状态、实例列表或连接弹窗后重试"
                    self.update(status="ready", selected=None, mappings=[], message=message)
            if not self.stopping.is_set():
                self.update(message="导入会话已到 30 分钟，请重新打开并登录")
        except Exception as exc:
            self.update(message=str(exc) if isinstance(exc, ValueError) else "浏览器不可用，请检查 Edge 和 Playwright 安装")
        finally:
            try:
                browser.close()
            finally:
                self.update(status="closed", candidates=[], selected=None, mappings=[])

    def close(self):
        self.stopping.set()
        self.update(status="closing" if self.thread and self.thread.is_alive() else "closed", candidates=[], selected=None, mappings=[])
        if self.thread:
            self.thread.join(timeout=2)

    def save(self, manager, payload):
        with self.lock:
            state = self.status()
            if self.stopping.is_set() or state["status"] != "ready" or payload["session_id"] != state.get("session_id"):
                raise ValueError("导入会话已结束，请重新读取实例")
            eid = payload["platform_id"]
            if state.get("selected") != eid:
                raise ValueError("连接信息不属于当前选中的实例")
            candidate = next(c for c in state["candidates"] if c["platform_id"] == eid)
            index = payload.get("mapping_index")
            if type(index) is not int or not 0 <= index < len(state.get("mappings", [])):
                raise ValueError("请选择有效的 SSH 映射")
            mapping = state["mappings"][index]
            value = dict(payload["instance"])
            username = value["username"].strip()
            source = {"origin": ORIGIN, "platform_id": eid, "account": username}
            existing = next((i for i in manager.list_instances() if all((i.get("portal") or {}).get(k) == v for k, v in source.items())), None)
            target = payload.get("target_instance_id")
            if target:
                target_instance = manager.store.get("instance", target)
                if existing and existing["id"] != target:
                    raise ValueError("该平台实例已经导入，请更新已有对应实例")
                if target_instance["username"] != username:
                    raise ValueError("更新目标的 SSH 用户名与当前账号不一致")
                if target_instance.get("portal") and any(target_instance["portal"].get(k) != v for k, v in source.items()):
                    raise ValueError("更新目标绑定了其他平台实例，请创建新记录")
                existing = target_instance
            source.update(observed_at=candidate["observed_at"], remaining_text=candidate["remaining_text"], expiry_estimated=candidate["expiry_estimated"])
            key = existing["id"] if existing else "school-" + hashlib.sha256((ORIGIN + "\n" + username + "\n" + eid).encode()).hexdigest()[:20]
            value.update(id=key, username=username, portal=source, **mapping)
            if candidate["expires_at"]:
                value["expires_at"] = candidate["expires_at"]
            password = payload.get("password")
            reuse = payload.get("credential_instance_id")
            if not password and reuse:
                donor = manager.store.get("instance", reuse)
                if donor["username"] != username:
                    raise ValueError("只能复用相同 SSH 用户名的凭据")
                password = manager.credentials.get(reuse)
            if not password:
                try:
                    manager.credentials.get(key)
                except (KeyError, ValueError):
                    raise ValueError("首次导入请填写 SSH 密码，或选择已有账号凭据") from None
            return manager.save_instance(value, password)
