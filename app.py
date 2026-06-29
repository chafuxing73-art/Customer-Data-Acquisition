import json
import logging
import os
import re
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

import requests
from flask import Flask, render_template, request, session


BASE_DIR = Path(__file__).resolve().parent
REQUEST_CAPTURE_FILE = os.getenv(
    "ONETOUCH_REQUEST_CAPTURE_FILE",
    "tmp/[3656] request_onetouch-partner.alibaba.com_message.txt",
)
RESPONSE_FIXTURE_FILE = os.getenv(
    "ONETOUCH_RESPONSE_FIXTURE_FILE",
    "tmp/[3656] response_onetouch-partner.alibaba.com_message.txt",
)
# 企微文档webhook地址配置
WEDOC_WEBHOOK_URLS = {
    "gangqian": os.getenv(
        "WEDOC_WEBHOOK_URL_GANGQIAN",
        "https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook"
        "?key=3OeXQfxTesRYhrHFn8IvM4Q8egvx1oal4aVhfBJ8jfo5xvh1CpJ2zkEsW5aGLcGnrg4djwooLpMmLZrtqQBopCZqxIhPzEww4SrZTkDDtUza",
    ),
    "ganghou": os.getenv(
        "WEDOC_WEBHOOK_URL_GANGHOU",
        "https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook"
        "?key=40191jy97AUh0NW33OPnPGR42GqmzVN84s4BfhXrLTq2HOTodAWacGBhRma4HBa1u6vtlOeGoMiYSzkYQZMQwOSamvNA3kEwF0TLgX8QWBhV",
    ),
}

# 为了保持向后兼容，保留原变量
WEDOC_WEBHOOK_URL = WEDOC_WEBHOOK_URLS["gangqian"]
ONETOUCH_BASE_URL = "https://onetouch-partner.alibaba.com"
ONETOUCH_PATH = "/ptnBase/luyou/express/order/list.json"
ONETOUCH_DETAIL_PATH = "/ptnBase/luyou/express/detail/getDetailByOrderId.json"
DEFAULT_ORDER_NUMBER = ""

# 钉钉机器人 Webhook 地址
DINGTALK_WEBHOOK_URL = "https://oapi.dingtalk.com/robot/send?access_token=54604769efe45d22da4f654c1946d170a464e5d039824c22f0a84b5284c5fc97"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "logs" / "app.log", encoding="utf-8"),
    ],
)
LOGGER = logging.getLogger("wedoc_web_app")


app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.secret_key = "your-secret-key-here"  # 添加secret key用于session管理


# ─── 配置加载 ───────────────────────────────────────────────────────────
def _load_config() -> Dict[str, Any]:
    config_path = BASE_DIR / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            LOGGER.warning("config.json 解析失败，使用默认配置")
    return {}


APP_CONFIG = _load_config()


# ─── 会话管理器（Keep-Alive + 自动重登录） ──────────────────────────────
_login_progress: Dict[str, Any] = {
    "status": "idle",
    "message": "",
    "screenshot_path": None,
    "started_at": None,
}


def _update_login_progress(status: str, message: str, screenshot_path: Optional[str] = None) -> None:
    global _login_progress
    _login_progress = {
        "status": status,
        "message": message,
        "screenshot_path": screenshot_path,
        "started_at": _login_progress.get("started_at") if status != "idle" else None,
    }
    LOGGER.info(f"[登录进度] {status}: {message}")


def get_login_progress() -> Dict[str, Any]:
    progress = dict(_login_progress)
    if progress.get("screenshot_path"):
        progress["screenshot_url"] = "/api/session/login-screenshot"
    return progress


class SessionManager:
    """管理阿里巴巴登录会话，提供 Keep-Alive 保活和自动重登录功能"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cookies: Dict[str, str] = {}
        self._ctoken: Optional[str] = None
        self._tb_token: Optional[str] = None
        self._last_refresh: Optional[datetime] = None
        self._is_expired: bool = True
        self._keep_alive_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_logging_in = False

    @property
    def is_valid(self) -> bool:
        return not self._is_expired and self._ctoken is not None

    @property
    def last_refresh(self) -> Optional[datetime]:
        return self._last_refresh

    @property
    def ctoken(self) -> Optional[str]:
        return self._ctoken

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "ctoken": self._ctoken if self._ctoken else None,
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "cookies_count": len(self._cookies),
        }

    def load_from_capture_file(self) -> bool:
        """从抓包文件加载凭证"""
        capture_path = BASE_DIR / REQUEST_CAPTURE_FILE
        if not capture_path.exists():
            return False
        try:
            _, headers, _, ctoken, tb_token = load_request_capture(capture_path)
            if not ctoken:
                return False
            # 从headers中提取cookies
            cookies = {}
            for key, value in headers.items():
                if key.lower() == "cookie":
                    for cookie_pair in value.split(";"):
                        cookie_pair = cookie_pair.strip()
                        if "=" in cookie_pair:
                            name, _, val = cookie_pair.partition("=")
                            cookies[name.strip()] = val.strip()
            with self._lock:
                self._cookies = cookies
                self._ctoken = ctoken
                self._tb_token = tb_token
                self._is_expired = False
                self._last_refresh = datetime.now()
            LOGGER.info(f"从抓包文件加载凭证成功，ctoken: {ctoken[:10]}...")
            return True
        except Exception as exc:
            LOGGER.warning(f"从抓包文件加载凭证失败: {exc}")
            return False

    def check_alive(self) -> bool:
        """检查当前会话是否有效"""
        if not self._ctoken:
            self._is_expired = True
            return False
        try:
            check_url = APP_CONFIG.get("keep_alive", {}).get(
                "check_url",
                "https://onetouch-partner.alibaba.com/ptnBase/luyou/logistics/listCustomersByPartnerId.json"
            )
            tb_token = self._tb_token or ""
            url = f"{check_url}?_tb_token_={tb_token}&ctoken={self._ctoken}"
            session = requests.Session()
            session.verify = False
            session.cookies.update(self._cookies)
            # 添加必要的请求头，模拟真实浏览器
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://onetouch-partner.alibaba.com/ptnBase/luyou/express/list.htm",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            })
            resp = session.get(url, timeout=15)
            result = resp.json()
            if result.get("code") == 838185:
                LOGGER.info("会话已过期（服务端返回838185）")
                self._is_expired = True
                return False
            LOGGER.info("会话保活检测：有效")
            self._is_expired = False
            return True
        except Exception as exc:
            LOGGER.warning(f"会话保活检测失败: {exc}")
            self._is_expired = True
            return False

    def refresh_login(self, username: str = None, password: str = None, wait_for_captcha: bool = True, remote_debug_port: int = None) -> bool:
        """自动重新登录，使用浏览器自动化方式

        Args:
            username: 可选的用户名，不提供则使用配置文件中的账号
            password: 可选的密码，不提供则使用配置文件中的密码
            wait_for_captcha: 是否等待用户手动完成验证码
            remote_debug_port: 本地Chrome远程调试端口，用于服务器环境连接本地浏览器

        Returns:
            True 表示登录成功，False 表示登录失败
        """
        if self._is_logging_in:
            LOGGER.info("已有登录任务进行中，跳过")
            return self.is_valid

        self._is_logging_in = True
        _update_login_progress("running", "正在准备登录...", None)
        try:
            cfg = APP_CONFIG.get("alibaba", {})
            uname = username or cfg.get("username", "")
            pwd = password or cfg.get("password", "")
            if not uname or not pwd:
                LOGGER.error("未配置登录凭证，无法自动重登录")
                _update_login_progress("failed", "未配置登录凭证，请在config.json中设置账号密码")
                return False

            LOGGER.info(f"开始浏览器自动化登录，账号: {uname}, 等待验证码: {wait_for_captcha}, 远程调试端口: {remote_debug_port}")
            try:
                cookies = login_alibaba(uname, pwd, wait_for_captcha=wait_for_captcha, remote_debug_port=remote_debug_port)
                ctoken = get_ctoken_from_cookies(cookies)
                if ctoken:
                    with self._lock:
                        self._cookies = cookies
                        self._ctoken = ctoken
                        self._tb_token = cookies.get("_tb_token_", "")
                        self._is_expired = False
                        self._last_refresh = datetime.now()
                    LOGGER.info(f"浏览器自动重登录成功，ctoken: {ctoken[:10]}...")
                    _update_login_progress("success", "登录成功，凭证已更新")
                    return True
                else:
                    _update_login_progress("failed", "登录成功但未获取到ctoken，请尝试新标签页登录")
            except Exception as exc:
                LOGGER.error(f"浏览器自动重登录异常: {exc}")
                _update_login_progress("failed", f"自动登录失败: {str(exc)[:100]}")

            LOGGER.error("浏览器登录失败")
            return False
        except Exception as exc:
            LOGGER.error(f"自动重登录异常: {exc}")
            _update_login_progress("failed", f"登录异常: {str(exc)[:100]}")
            return False
        finally:
            self._is_logging_in = False

    def ensure_valid(self, username: str = None, password: str = None) -> bool:
        """确保会话有效，如果过期则自动重登录"""
        if self.is_valid and self.check_alive():
            return True
        LOGGER.info("会话已失效，触发自动重登录...")
        return self.refresh_login(username, password)

    def get_session_cookies(self) -> Dict[str, str]:
        return self._cookies.copy()

    def get_ctoken(self) -> Optional[str]:
        return self._ctoken

    def get_tb_token(self) -> Optional[str]:
        return self._tb_token

    def start_keep_alive(self):
        """启动后台 Keep-Alive 保活线程"""
        if self._keep_alive_thread and self._keep_alive_thread.is_alive():
            return
        interval = APP_CONFIG.get("keep_alive", {}).get("interval_minutes", 15)
        LOGGER.info(f"启动 Keep-Alive 保活线程，间隔 {interval} 分钟")

        def _keep_alive_loop():
            while not self._stop_event.is_set():
                self._stop_event.wait(interval * 60)
                if self._stop_event.is_set():
                    break
                try:
                    LOGGER.info("Keep-Alive: 开始保活检测...")
                    alive = self.check_alive()
                    if alive:
                        age_minutes = 0
                        if self._last_refresh:
                            age_minutes = (datetime.now() - self._last_refresh).total_seconds() / 60
                        LOGGER.info(f"Keep-Alive: 会话有效，已持续 {age_minutes:.0f} 分钟")
                        if age_minutes > 120:
                            LOGGER.info(f"Keep-Alive: 会话已持续 {age_minutes:.0f} 分钟，主动刷新以延长有效期...")
                            self.refresh_login()
                    else:
                        LOGGER.info("Keep-Alive: 会话已过期，尝试自动重登录...")
                        self.refresh_login()
                except Exception as exc:
                    LOGGER.error(f"Keep-Alive 异常: {exc}")

        self._keep_alive_thread = threading.Thread(target=_keep_alive_loop, daemon=True)
        self._keep_alive_thread.start()

    def stop_keep_alive(self):
        """停止 Keep-Alive 保活线程"""
        self._stop_event.set()
        if self._keep_alive_thread:
            self._keep_alive_thread.join(timeout=5)
        LOGGER.info("Keep-Alive 保活线程已停止")


# 全局会话管理器实例
session_mgr = SessionManager()


def get_current_timestamp() -> str:
    """获取当前日期的时间戳（毫秒）"""
    return str(int(datetime.now().timestamp() * 1000))


# 港前工单schema定义
BASE_SCHEMA_DEFINITION: Dict[str, Dict[str, Any]] = {
    "fabcde": {"label": "登记日期", "source_field": None, "type": "text", "empty": get_current_timestamp},

    "flosGy": {"label": "订单号", "source_field": "orderNumber", "type": "text", "empty": ""},
    "f3Vstd": {"label": "下单账号", "source_field": "sellerLoginId", "type": "text", "empty": ""},
    "fyNfPg": {"label": "用户名称", "source_field": "customerName", "type": "text", "empty": ""},
    "f6KONI": {"label": "电话", "source_field": "mobileNo", "type": "text", "empty": ""},
    "fE2nO0": {"label": "邮箱", "source_field": "email", "type": "text", "empty": ""},
    "faGx63": {
        "label": "半托管/非半托管",
        "source_field": "solutionName",
        "type": "select",
        "empty": [{"text": "半托管"}],
    },
    "fA8znU": {"label": "非半托管问题类型", "source_field": None, "type": "select", "empty": []},
    "fg4QC7": {"label": "半托管问题类型", "source_field": None, "type": "select", "empty": []},
    "fhD9aC": {"label": "问题描述", "source_field": None, "type": "text", "empty": ""},
    "fp8HnB": {"label": "客户诉求", "source_field": None, "type": "text", "empty": ""},
    "fwpa5L": {"label": "附件", "source_field": None, "type": "text", "empty": []},
    "fz0a0D": {"label": "工单号", "source_field": None, "type": "text", "empty": ""},
    "f9pjS2": {"label": "机器人报错信息", "source_field": None, "type": "text", "empty": ""},
}

# 港后工单schema定义
GANGHOU_SCHEMA_DEFINITION: Dict[str, Dict[str, Any]] = {
    "fabcde": {"label": "登记日期", "source_field": None, "type": "text", "empty": get_current_timestamp},

    "frSPFh": {"label": "订单号", "source_field": "orderNumber", "type": "text", "empty": ""},
    "fsEWKh": {"label": "下单账号（会员名）", "source_field": "sellerLoginId", "type": "text", "empty": ""},
    "flKZSo": {"label": "用户名称", "source_field": "customerName", "type": "text", "empty": ""},
    "fNwkHC": {"label": "电话", "source_field": "mobileNo", "type": "text", "empty": ""},
    "foUoeA": {"label": "邮箱", "source_field": "email", "type": "text", "empty": ""},
    "fcsXrQ": {
        "label": "半托管/非半托管",
        "source_field": "solutionName",
        "type": "select",
        "empty": [{"text": "半托管"}],
    },
    "fYJhYN": {"label": "非半托管问题类型", "source_field": None, "type": "select", "empty": []},
    "f4PXOn": {"label": "半托管问题类型", "source_field": None, "type": "select", "empty": []},
    "fapRVM": {"label": "问题描述", "source_field": None, "type": "text", "empty": ""},
    "fqaQdG": {"label": "客户诉求", "source_field": None, "type": "text", "empty": ""},
    "fk4Kcy": {"label": "附件", "source_field": None, "type": "text", "empty": []},
    "fe410B": {"label": "工单号", "source_field": None, "type": "text", "empty": ""},
    "f7uJze": {"label": "机器人报错信息", "source_field": None, "type": "text", "empty": ""},
}

# 根据文档类型获取schema定义
def get_schema_definition(document: str) -> Dict[str, Dict[str, Any]]:
    if document == "ganghou":
        return GANGHOU_SCHEMA_DEFINITION
    return BASE_SCHEMA_DEFINITION

REQUIRED_FIELDS = [
    "email",
    "mobileNo",
    "sellerLoginId",
    "customerName",
    "solutionName",
    "orderNumber",
]


def ensure_runtime_dirs() -> None:
    (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "tmp").mkdir(parents=True, exist_ok=True)


def load_request_capture(path: Path) -> Tuple[str, Dict[str, str], Dict[str, str], Optional[str], Optional[str]]:
    raw_text = path.read_text(encoding="utf-8")
    lines = [line for line in raw_text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("请求抓包文件为空")

    request_line = lines[0]
    parts = request_line.split(" ")
    if len(parts) < 2:
        raise ValueError("请求行格式不正确")

    request_target = parts[1]
    parsed = urlsplit(request_target)
    query = parse_qs(parsed.query)
    ctoken = query.get("ctoken", [None])[0]
    tb_token = query.get("_tb_token_", [None])[0]

    headers: Dict[str, str] = {}
    cookie_parts: List[str] = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        header_name = name.strip().lower()
        header_value = value.strip()
        if header_name == "cookie":
            cookie_parts.append(header_value)
            continue
        if header_name in {"host", "content-length"}:
            continue
        headers[header_name] = header_value

    if cookie_parts:
        headers["cookie"] = "; ".join(cookie_parts)

    # 添加必要的请求头
    headers["Host"] = "onetouch-partner.alibaba.com"
    headers["Connection"] = "keep-alive"
    headers["Upgrade-Insecure-Requests"] = "1"
    headers["Sec-Fetch-Dest"] = "document"
    headers["Sec-Fetch-Mode"] = "navigate"
    headers["Sec-Fetch-Site"] = "none"
    headers["Sec-Fetch-User"] = "?1"
    headers["Sec-Ch-Ua"] = "\"Not_A Brand\";v=\"8\", \"Chromium\";v=\"120\", \"Microsoft Edge\";v=\"120\""
    headers["Sec-Ch-Ua-Mobile"] = "?0"
    headers["Sec-Ch-Ua-Platform"] = "\"Windows\""

    return request_target, headers, query, ctoken, tb_token


def build_query_json(order_number: str) -> Dict[str, Any]:
    return {
        "statusType": "ALL",
        "source": None,
        "generalSearchField": order_number,
        "queryNewList": True,
        "serviceType": None,
        "statusList": [],
        "queryPayStatus": None,
        "exceptionOrderStatus": None,
        "currentPage": 1,
        "pageSize": 10,
        "sort": {},
        "needConsignorAddress": True,
        "needConsigneeAddress": True,
    }


def fetch_order_data_live(order_number: str, username: str = None, password: str = None, wait_for_captcha: bool = True) -> Dict[str, Any]:
    session = requests.Session()
    session.verify = False
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://onetouch-partner.alibaba.com/ptnBase/luyou/express/list.htm",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Connection": "keep-alive",
    }
    
    ctoken = None

    # 优先使用 SessionManager 中的凭证
    if session_mgr.is_valid:
        ctoken = session_mgr.get_ctoken()
        session.cookies.update(session_mgr.get_session_cookies())
        LOGGER.info("使用 SessionManager 中的有效凭证")

    # 如果 SessionManager 无效，尝试自动重登录（仅本地环境）
    if not ctoken:
        if session_mgr.ensure_valid(username, password):
            ctoken = session_mgr.get_ctoken()
            session.cookies.update(session_mgr.get_session_cookies())
            LOGGER.info("通过自动重登录获取到新凭证")

    # 如果仍然没有ctoken，回退到抓包文件
    if not ctoken:
        capture_path = BASE_DIR / REQUEST_CAPTURE_FILE
        if not capture_path.exists():
            raise ValueError(f"请求抓包文件不存在: {capture_path}")
        _, capture_headers, _, ctoken, tb_token = load_request_capture(capture_path)
        if not ctoken:
            raise ValueError("请求抓包中缺少 ctoken，无法发起实时查询。服务器环境请使用前端'手动输入'功能更新凭证。")
        headers.update(capture_headers)
        LOGGER.info("使用抓包文件中的凭证")
    
    query_json = build_query_json(order_number)
    query_json_str = json.dumps(query_json, ensure_ascii=False, separators=(",", ":"))
    
    url = f"{ONETOUCH_BASE_URL}{ONETOUCH_PATH}?ctoken={ctoken}&json={query_json_str}"
    session.headers.update(headers)
    
    max_retries = 3
    retry_delay = 2
    
    for attempt in range(max_retries):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            result = response.json()
            
            if result.get("code") == 838185:
                LOGGER.warning("登录状态过期，尝试自动重登录...")
                auto_login_success = session_mgr.refresh_login(username, password, wait_for_captcha=wait_for_captcha)
                if auto_login_success:
                    ctoken = session_mgr.get_ctoken()
                    session.cookies.update(session_mgr.get_session_cookies())
                    url = f"{ONETOUCH_BASE_URL}{ONETOUCH_PATH}?ctoken={ctoken}&json={query_json_str}"
                    response = session.get(url, timeout=30)
                    result = response.json()
                else:
                    if session_mgr._last_refresh and session_mgr._ctoken:
                        raise ValueError(f"登录状态已过期（自动重登录失败），请重新获取最新的ctoken并使用'手动输入'功能更新，或点击'刷新登录'按钮")
                    else:
                        raise ValueError("登录状态已过期，自动登录需要验证码，请访问前端页面点击刷新登录按钮完成验证")

            data = result.get("data") or {}
            data_list = data.get("dataList") or []
            total = data.get("total", 0)

            if not data_list and total == 0 and attempt == 0:
                LOGGER.warning(f"查询返回空数据列表，可能是会话已过期（未返回838185错误），尝试验证会话...")
                if not session_mgr.check_alive():
                    LOGGER.warning("会话验证确认已过期，尝试自动重登录...")
                    session_mgr._is_expired = True
                    auto_login_success = session_mgr.refresh_login(username, password, wait_for_captcha=wait_for_captcha)
                    if auto_login_success:
                        ctoken = session_mgr.get_ctoken()
                        session.cookies.update(session_mgr.get_session_cookies())
                        url = f"{ONETOUCH_BASE_URL}{ONETOUCH_PATH}?ctoken={ctoken}&json={query_json_str}"
                        response = session.get(url, timeout=30)
                        result = response.json()
                    else:
                        raise ValueError("会话已过期且自动重登录失败，请在前端页面使用'新标签页登录'或'手动输入'功能更新凭证")
            
            LOGGER.info(f"成功获取订单 {order_number} 数据，尝试次数: {attempt + 1}")
            return result
            
        except requests.exceptions.RequestException as exc:
            LOGGER.warning(f"获取订单 {order_number} 数据失败，尝试次数: {attempt + 1}/{max_retries}, 错误: {str(exc)}")
            if attempt < max_retries - 1:
                _time.sleep(retry_delay)
                retry_delay *= 1.5
            else:
                LOGGER.error(f"获取订单 {order_number} 数据最终失败，已达到最大重试次数")
                raise


def fetch_order_data_fixture(order_number: str) -> Dict[str, Any]:
    fixture_path = BASE_DIR / RESPONSE_FIXTURE_FILE
    if not fixture_path.exists():
        raise ValueError(f"响应样例文件不存在: {fixture_path}")
    raw_text = fixture_path.read_text(encoding="utf-8")
    body_start = raw_text.find("{")
    if body_start < 0:
        raise ValueError("响应样例文件中未找到 JSON 包体")
    payload = json.loads(raw_text[body_start:])

    data = payload.get("data") or {}
    data_list = data.get("dataList", [])
    filtered = [item for item in data_list if item.get("orderNumber") == order_number]
    if "data" not in payload:
        payload["data"] = {}
    payload["data"]["dataList"] = filtered
    payload["data"]["total"] = len(filtered)
    return payload


def fetch_order_detail(order_id: str) -> Optional[Dict[str, Any]]:
    if not session_mgr.is_valid:
        LOGGER.warning("会话无效，无法获取订单详情")
        return None

    try:
        session = requests.Session()
        session.verify = False
        session.cookies.update(session_mgr.get_session_cookies())

        tb_token = session_mgr.get_tb_token() or ""
        ctoken = session_mgr.get_ctoken() or ""

        url = f"{ONETOUCH_BASE_URL}{ONETOUCH_DETAIL_PATH}?_tb_token_={tb_token}&ctoken={ctoken}"

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": f"{ONETOUCH_BASE_URL}/ptnBase/luyou/express/detail.htm?id={order_id}",
            "Origin": ONETOUCH_BASE_URL,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        session.headers.update(headers)

        data = {"orderId": order_id}
        response = session.post(url, data=data, timeout=30)
        response.raise_for_status()
        result = response.json()

        if result.get("code") == 838185:
            LOGGER.warning("查询订单详情时登录状态过期")
            return None

        LOGGER.info(f"成功获取订单详情，orderId: {order_id}")
        return result
    except Exception as exc:
        LOGGER.warning(f"获取订单详情失败: {exc}")
        return None


def _extract_logistics_company_name(detail: Dict[str, Any]) -> str:
    def _search(obj: Any) -> str:
        if isinstance(obj, dict):
            if "logisticsCompanyName" in obj and obj["logisticsCompanyName"]:
                return str(obj["logisticsCompanyName"])
            for v in obj.values():
                result = _search(v)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = _search(item)
                if result:
                    return result
        return ""

    return _search(detail)


# ============================================================
# 字段翻译/映射表 - 确保所有英文枚举值都转为中文
# ============================================================

# 退货方式映射
RETURN_TYPE_MAP = {
    "PLATFORM_RETURN": "平台退货",
    "MERCHANT_RETURN": "商家退货",
    "SELF_RETURN": "自主退货",
    "SELF_PICKUP": "自提退货",
    "SELF_SEND": "自行寄回",
    "REFUND_ONLY": "仅退款",
    "ONLY_REFUND": "仅退款",
    "REFUND": "仅退款",
    "RETURN_REFUND": "退货退款",
    "AFTERSALE_RETURN": "售后退货",
    "PICKUP_RETURN": "上门取件退货",
    "NO_NEED_RETURN": "无需退货",
    "BUYER_RETURN": "买家退货",
    "SELLER_RETURN": "卖家退货",
}

# 订单状态映射
ORDER_STATUS_MAP = {
    "PENDING_PAYMENT": "待付款",
    "WAIT_PAY": "待付款",
    "PENDING_SHIPMENT": "待发货",
    "WAIT_SHIP": "待发货",
    "PENDING_DELIVERY": "待揽收",
    "WAIT_COLLECT": "待揽收",
    "PENDING_RECEIPT": "待签收",
    "WAIT_RECEIVE": "待签收",
    "COMPLETED": "已完成",
    "FINISHED": "已完成",
    "CLOSED": "已关闭",
    "CANCELLED": "已取消",
    "CANCELED": "已取消",
    "RETURNING": "退货中",
    "RETURNED": "已退货",
    "REFUNDING": "退款中",
    "REFUNDED": "已退款",
    "ORDER_TERMINATED": "订单终止",
    "TERMINATED": "订单终止",
    "SHIPPED": "已发货",
    "IN_TRANSIT": "运输中",
    "DELIVERED": "已签收",
    "SIGNED": "已签收",
    "PARTIAL_SHIPPED": "部分发货",
    "PARTIAL_DELIVERED": "部分签收",
}

# 物流状态映射
LOGISTICS_STATUS_MAP = {
    "CREATED": "已创建",
    "PICKED": "已揽收",
    "IN_TRANSIT": "运输中",
    "DELIVERING": "派送中",
    "SIGNED": "已签收",
    "FAILED": "签收失败",
    "RETURNING": "退回中",
    "RETURNED": "已退回",
    "EXCEPTION": "异常",
}

# 订单类型映射
SOLUTION_TYPE_MAP = {
    "FULL_MANAGED": "全托管",
    "SEMI_MANAGED": "半托管",
    "HALF_MANAGED": "半托管",
    "SELF_SHIP": "自发货",
    "NON_MANAGED": "非半托管",
    "NORMAL": "非半托管",
}

# 通用关键词翻译（用于兜底）
KEYWORD_TRANSLATIONS = {
    "return": "退货",
    "refund": "退款",
    "pickup": "取件",
    "delivery": "配送",
    "ship": "发货",
    "order": "订单",
    "status": "状态",
    "pending": "待",
    "waiting": "等待",
    "complete": "完成",
    "finished": "已完成",
    "closed": "关闭",
    "cancel": "取消",
    "self": "自",
    "platform": "平台",
    "merchant": "商家",
    "buyer": "买家",
    "seller": "卖家",
    "only": "仅",
    "partial": "部分",
    "full": "全",
    "semi": "半",
    "managed": "托管",
    "in transit": "运输中",
    "signed": "已签收",
    "delivered": "已送达",
    "exception": "异常",
}


def translate_enum_value(value: str, mapping: Dict[str, str]) -> str:
    """
    通用枚举值翻译：先精确匹配，再模糊匹配，最后关键词翻译
    """
    if not value or not isinstance(value, str):
        return value
    val = value.strip()
    if not val:
        return value

    val_upper = val.upper()

    # 1. 精确匹配
    if val_upper in mapping:
        return mapping[val_upper]

    # 2. 模糊匹配（子串包含）
    for key, label in mapping.items():
        if key in val_upper or val_upper in key:
            return label

    return val  # 返回原值，由调用方进一步处理


def smart_translate(text: str, context: str = "") -> str:
    """
    智能翻译兜底：将英文关键词翻译为中文
    适用于未在映射表中的英文短语
    """
    if not text or not isinstance(text, str):
        return text

    # 如果已经是纯中文（不含英文字母），直接返回
    if not re.search(r'[a-zA-Z]', text):
        return text

    result = text
    result_upper = result.upper()

    # 按关键词替换（长词优先）
    sorted_keywords = sorted(KEYWORD_TRANSLATIONS.keys(), key=len, reverse=True)
    for keyword in sorted_keywords:
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        result = pattern.sub(KEYWORD_TRANSLATIONS[keyword], result)

    # 清理多余的下划线和空格
    result = result.replace('_', '').replace('  ', ' ').strip()

    return result if result else text


def ensure_chinese(text: str, field_name: str = "") -> str:
    """
    确保文本是中文：如果检测到英文字母且不是ID/账号/邮箱/电话等，尝试翻译
    """
    if not text or not isinstance(text, str):
        return text or "-"

    # 跳过不需要翻译的字段类型
    skip_fields = [
        "orderNumber", "tradeBizId", "sellerLoginId", "customerName",
        "email", "mobileNo", "phone", "senderAddress", "address",
        "logisticsCompanyName", "zip",
    ]
    if field_name in skip_fields:
        return text

    # 如果是纯数字或字母数字组合（如订单号、ID），不翻译
    if re.match(r'^[A-Za-z0-9\-]+$', text) and len(text) >= 8:
        return text

    # 如果包含中文字符，视为已翻译
    if re.search(r'[\u4e00-\u9fa5]', text):
        return text

    # 尝试智能翻译
    translated = smart_translate(text)
    return translated if translated != text else text


def _extract_return_type(detail: Dict[str, Any]) -> str:
    """从订单详情中提取退货方式"""
    data = detail.get("data") or detail
    return_order = data.get("returnOrderDTO")
    if not return_order:
        return ""
    return_type = str(return_order.get("returnType") or "").strip()
    if not return_type:
        return ""
    # 先精确+模糊匹配映射表
    result = translate_enum_value(return_type, RETURN_TYPE_MAP)
    # 如果返回的还是英文，用智能翻译兜底
    if result and not re.search(r'[\u4e00-\u9fa5]', result):
        result = smart_translate(result)
    return result


def find_order_record(payload: Dict[str, Any], order_number: str) -> Optional[Dict[str, Any]]:
    data = payload.get("data") or {}
    data_list = data.get("dataList") or []
    if not data_list:
        code = payload.get("code")
        msg = payload.get("message", "")
        LOGGER.warning(f"查询结果为空: code={code}, message={msg}, total={data.get('total', 0)}")
    for item in data_list:
        if item.get("orderNumber") == order_number or item.get("tradeBizId") == order_number:
            return item
    return None


def find_order_records(payload: Dict[str, Any], order_number: str) -> List[Dict[str, Any]]:
    data = payload.get("data") or {}
    records = []
    for item in data.get("dataList", []):
        if item.get("orderNumber") == order_number or item.get("tradeBizId") == order_number:
            records.append(item)
    return records


def extract_business_data(record: Dict[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    contact = record.get("consignorAddress", {}).get("contact", {})
    address = record.get("consignorAddress", {}).get("address", {})
    source_solution_name = str(record.get("solutionName") or "")
    solution_name = "半托管" if "半托管" in source_solution_name else "非半托管"

    order_status_desc = str(record.get("orderStatusDesc") or "")
    order_status_code = str(record.get("orderStatus") or record.get("status") or "")
    if order_status_desc == "订单关闭":
        order_status = "已关闭"
    elif order_status_desc:
        order_status = order_status_desc
    elif order_status_code:
        order_status = translate_enum_value(order_status_code, ORDER_STATUS_MAP)
        order_status = ensure_chinese(order_status, "orderStatus")
    else:
        order_status = "-"

    # 组装发件地址: 邮编，详细地址，区，市，省，国家
    zip_code = str(address.get("zip") or "")
    detail_address = str(address.get("address") or "")
    district = str(address.get("district", {}).get("name") or "")
    city = str(address.get("city", {}).get("name") or "")
    province = str(address.get("province", {}).get("name") or "")
    country = str(address.get("country", {}).get("name") or "")
    sender_address = f"{zip_code}，{detail_address}，{district}，{city}，{province}，{country}"

    extracted = {
        "email": str(contact.get("email") or ""),
        "mobileNo": str(contact.get("mobileNo") or ""),
        "sellerLoginId": str(record.get("sellerLoginId") or ""),
        "customerName": str(record.get("customerName") or ""),
        "solutionName": solution_name,
        "orderNumber": str(record.get("orderNumber") or ""),
        "tradeBizId": str(record.get("tradeBizId") or ""),
        "orderStatus": order_status,
        "logisticsCompanyName": "",
        "senderAddress": sender_address,
    }
    missing_fields = [field for field in REQUIRED_FIELDS if not extracted.get(field)]
    return extracted, missing_fields


def build_schema_definition(extra_empty_fields: List[str], document: str = "gangqian") -> Dict[str, Dict[str, Any]]:
    schema = dict(get_schema_definition(document))
    for field_id in extra_empty_fields:
        schema[field_id] = {
            "label": f"额外空字段:{field_id}",
            "source_field": None,
            "type": "text",
            "empty": "",
        }
    return schema


def build_wedoc_values(
    extracted: Dict[str, str],
    schema_definition: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for field_id, config in schema_definition.items():
        source_field = config["source_field"]
        if source_field is None:
            # 检查empty是否为函数，如果是则调用它
            if callable(config["empty"]):
                values[field_id] = config["empty"]()
            else:
                values[field_id] = config["empty"]
            continue

        raw_value = extracted.get(source_field, "")
        if field_id == "faGx63" or field_id == "fcsXrQ":
            values[field_id] = [{"text": raw_value}] if raw_value else config["empty"]
        elif field_id == "f6KONI" or field_id == "fNwkHC":
            # 将电话字段转换为纯数字
            if raw_value:
                try:
                    values[field_id] = int(raw_value)
                except ValueError:
                    values[field_id] = config["empty"]
            else:
                values[field_id] = config["empty"]
        else:
            values[field_id] = raw_value if raw_value else config["empty"]
    return values


def build_preview_payload(values: Dict[str, Any], schema_definition: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    # 使用与企业微信文档API兼容的schema
    schema = {
        "fabcde": "登记日期",
        "flosGy": "订单号",
        "f3Vstd": "下单账号（会员名）",
        "fyNfPg": "用户名称",
        "f6KONI": "电话",
        "fE2nO0": "邮箱",
        "faGx63": "半托管/非半托管",
        "fA8znU": "非半托管问题类型",
        "fg4QC7": "半托管问题类型",
        "fhD9aC": "问题描述",
        "fp8HnB": "客户诉求",
        "fwpa5L": "附件",
        "fz0a0D": "工单号",
        "f9pjS2": "机器人报错信息"
    }
    return {"schema": schema, "add_records": [{"values": values}]}


def build_webhook_payload(values: Dict[str, Any]) -> Dict[str, Any]:
    # 构造企业微信文档API所需的请求数据
    schema = {
        "fabcde": "登记日期",
        "flosGy": "订单号",
        "f3Vstd": "下单账号（会员名）",
        "fyNfPg": "用户名称",
        "f6KONI": "电话",
        "fE2nO0": "邮箱",
        "faGx63": "半托管/非半托管",
        "fA8znU": "非半托管问题类型",
        "fg4QC7": "半托管问题类型",
        "fhD9aC": "问题描述",
        "fp8HnB": "客户诉求",
        "fwpa5L": "附件",
        "fz0a0D": "工单号",
        "f9pjS2": "机器人报错信息"
    }
    return {"schema": schema, "add_records": [{"values": values}]}


def send_to_wedoc(payload: Dict[str, Any], document: str = "gangqian") -> Dict[str, Any]:
    webhook_url = WEDOC_WEBHOOK_URLS.get(document, WEDOC_WEBHOOK_URLS["gangqian"])
    max_retries = 3
    retry_delay = 2  # 秒
    
    for attempt in range(max_retries):
        try:
            response = requests.post(webhook_url, json=payload, timeout=30, verify=False)
            response.raise_for_status()
            LOGGER.info(f"成功发送数据到 {document} 文档，尝试次数: {attempt + 1}")
            return response.json()
        except requests.exceptions.RequestException as exc:
            LOGGER.warning(f"发送数据到 {document} 文档失败，尝试次数: {attempt + 1}/{max_retries}, 错误: {str(exc)}")
            if attempt < max_retries - 1:
                import time
                time.sleep(retry_delay)
                retry_delay *= 1.5  # 指数退避
            else:
                LOGGER.error(f"发送数据到 {document} 文档最终失败，已达到最大重试次数")
                raise


def split_extra_fields(raw_value: str) -> List[str]:
    fields = []
    for part in raw_value.replace("，", ",").split(","):
        field_id = part.strip()
        if field_id:
            fields.append(field_id)
    return fields


def login_alibaba(username: str, password: str, wait_for_captcha: bool = True, remote_debug_port: int = None) -> Dict[str, str]:
    """使用浏览器自动化登录阿里巴巴，获取cookie信息

    Args:
        username: 阿里巴巴账号
        password: 阿里巴巴密码
        wait_for_captcha: 是否等待用户手动完成验证码（True=等待，False=遇到验证码立即失败）
        remote_debug_port: 本地Chrome远程调试端口，提供此端口则连接本地Chrome而非服务器Chromium

    Returns:
        登录后的cookies字典
    """
    import time as _time
    import os

    try:
        from DrissionPage import ChromiumPage, ChromiumOptions
    except ImportError:
        raise ImportError("需要安装 DrissionPage 库: pip install DrissionPage")

    page = None
    use_remote = False
    try:
        LOGGER.info(f"开始浏览器自动化登录阿里巴巴，账号: {username}, 等待验证码: {wait_for_captcha}")

        is_server_env = os.path.exists('/usr/bin/chromium') or os.path.exists('/usr/bin/chromium-browser')

        if remote_debug_port:
            LOGGER.info(f"使用本地Chrome远程调试模式，端口: {remote_debug_port}")
            _update_login_progress("running", f"正在连接本地Chrome（端口:{remote_debug_port}）...")
            use_remote = True
            co = ChromiumOptions()
            co.set_argument(f'--remote-debugging-port={remote_debug_port}')
            page = ChromiumPage(f'127.0.0.1:{remote_debug_port}')
            LOGGER.info("成功连接到本地Chrome浏览器")
        else:
            _update_login_progress("running", "正在启动浏览器...")
            co = ChromiumOptions()

            # 兼容新旧版本 DrissionPage API
            _headless_val = not wait_for_captcha if not is_server_env else False

            if is_server_env:
                co.set_argument('--headless=new')
                co.set_argument('--no-sandbox')
                co.set_argument('--disable-gpu')
                co.set_argument('--disable-blink-features=AutomationControlled')
                co.set_argument('--disable-dev-shm-usage')
                co.set_argument('--window-size=1920,1080')
                co.set_argument('--disable-extensions')
                co.set_argument('--disable-software-rasterizer')
                co.set_argument('--hide-scrollbars')
                co.set_argument('--mute-audio')
                co.set_argument('--disable-web-security')
                co.set_argument('--allow-running-insecure-content')
                chromium_path = os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium')
                if os.path.exists(chromium_path):
                    co.set_browser_path(chromium_path)
                LOGGER.info(f"服务器环境：使用headless=new模式 + Chromium: {chromium_path}")
            else:
                if isinstance(getattr(type(co), 'headless', None), property):
                    co.headless = _headless_val
                else:
                    co.headless(_headless_val)
                co.set_argument('--no-sandbox')
                co.set_argument('--disable-gpu')
                co.set_argument('--disable-blink-features=AutomationControlled')
                if not wait_for_captcha:
                    co.set_argument('--disable-dev-shm-usage')
                    co.set_argument('--window-size=1920,1080')
                LOGGER.info("本地环境：使用默认浏览器")

            # 设置User-Agent
            try:
                co.set_user_agent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0')
            except AttributeError:
                co.set_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0')

            # 创建页面对象 - 兼容新旧版本
            try:
                page = ChromiumPage(addr_or_opts=co)
            except TypeError:
                try:
                    page = ChromiumPage(co)
                except TypeError:
                    page = ChromiumPage()
            except Exception as browser_err:
                if is_server_env and 'connection fails' in str(browser_err).lower():
                    LOGGER.warning(f"Chromium连接失败，尝试指定端口重试: {browser_err}")
                    try:
                        import random
                        retry_port = random.randint(9300, 9400)
                        co.set_local_port(retry_port)
                        LOGGER.info(f"重试使用端口: {retry_port}")
                        page = ChromiumPage(addr_or_opts=co)
                    except Exception as retry_err:
                        raise ValueError(f"Chromium浏览器无法启动，请检查服务器Chromium安装状态。错误: {retry_err}")
                else:
                    raise

        page.get('https://login.alibaba.com/mini_login.htm?lang=zh_CN&appName=nemesis&appEntrance=alibaba&styleType=vertical')
        _update_login_progress("running", "正在加载登录页面...")
        _time.sleep(5)

        _update_login_progress("running", "正在填写账号密码...")
        login_input = page.ele('tag:input@name=account', timeout=15)
        if not login_input:
            raise ValueError("未找到账号输入框")
        login_input.clear()
        login_input.input(username)
        _time.sleep(1)

        pwd_input = page.ele('tag:input@name=password', timeout=5)
        if not pwd_input:
            raise ValueError("未找到密码输入框")
        pwd_input.clear()
        pwd_input.input(password)
        _time.sleep(1)

        login_btn = page.ele('text:登录', timeout=5)
        if not login_btn:
            raise ValueError("未找到登录按钮")
        login_btn.click()
        _update_login_progress("running", "已点击登录按钮，等待登录完成...")
        LOGGER.info("已点击登录按钮，等待登录完成...")

        max_wait = 60 if wait_for_captcha else 30
        poll_interval = 2
        waited = 0
        login_success = False

        while waited < max_wait:
            _time.sleep(poll_interval)
            waited += poll_interval

            current_url = page.url or ""

            if "login.alibaba.com" not in current_url:
                login_success = True
                _update_login_progress("running", "登录成功，正在获取凭证...")
                LOGGER.info(f"登录成功，已跳转到: {current_url[:80]}")
                break

            captcha_selectors = [
                'tag:iframe@src:=captcha',
                'tag:div@class:baxia-dialog',
                'tag:div@id=baxia-dialog-content',
                '#nc_1_wrapper',
                '.nc-container',
                'tag:div@class:=slider',
            ]
            captcha_found = False
            for selector in captcha_selectors:
                try:
                    ele = page.ele(selector, timeout=0.5)
                    if ele:
                        captcha_found = True
                        break
                except Exception:
                    pass

            if captcha_found:
                if is_server_env and not use_remote:
                    try:
                        screenshot_path = str(BASE_DIR / "tmp" / "login_screenshot.png")
                        page.get_screenshot(path=screenshot_path)
                    except Exception:
                        try:
                            page.get_screenshot(filename=screenshot_path)
                        except Exception:
                            screenshot_path = None

                    LOGGER.warning("服务器环境检测到验证码，尝试自动滑块验证...")
                    _update_login_progress("running", "检测到验证码，正在尝试自动处理...", screenshot_path)

                    try:
                        slider_btn = page.ele('tag:div@class:=btn_slide', timeout=3)
                        if not slider_btn:
                            slider_btn = page.ele('#nc_1_n1z', timeout=2)
                        if not slider_btn:
                            slider_btn = page.ele('.nc_iconfont.btn_slide', timeout=2)
                        if slider_btn:
                            from DrissionPage.common import Actions
                            actions = Actions(page)
                            actions.move_to(slider_btn)
                            actions.hold()
                            _time.sleep(0.5)
                            actions.move(300, 0, duration=1.5)
                            _time.sleep(0.5)
                            actions.release()
                            LOGGER.info("已尝试自动滑块验证")
                            _time.sleep(3)
                    except Exception as slide_err:
                        LOGGER.warning(f"自动滑块验证失败: {slide_err}")

                    captcha_wait = 0
                    while captcha_wait < 15:
                        _time.sleep(3)
                        captcha_wait += 3
                        current_url = page.url or ""
                        if "login.alibaba.com" not in current_url:
                            login_success = True
                            _update_login_progress("running", "验证码通过，登录成功！正在获取凭证...")
                            LOGGER.info("服务器环境验证码通过，登录成功！")
                            break

                    if not login_success:
                        _update_login_progress("failed", "验证码自动处理失败，请使用\"新标签页登录\"或\"手动输入\"功能", screenshot_path)
                        raise ValueError("服务器环境验证码自动处理失败，请使用\"新标签页登录\"或\"手动输入\"功能")
                    break
                elif wait_for_captcha or use_remote:
                    _update_login_progress("running", "检测到验证码！请在浏览器窗口中手动完成验证...")
                    LOGGER.warning("检测到验证码！请在浏览器窗口中手动完成验证...")
                    captcha_wait = 0
                    while captcha_wait < 120:
                        _time.sleep(3)
                        captcha_wait += 3
                        current_url = page.url or ""
                        if "login.alibaba.com" not in current_url:
                            login_success = True
                            _update_login_progress("running", "验证码通过，登录成功！正在获取凭证...")
                            LOGGER.info("验证码通过，登录成功！")
                            break
                        captcha_still = False
                        for selector in captcha_selectors:
                            try:
                                ele = page.ele(selector, timeout=0.5)
                                if ele:
                                    captcha_still = True
                                    break
                            except Exception:
                                pass
                        if not captcha_still:
                            _time.sleep(5)
                            current_url = page.url or ""
                            if "login.alibaba.com" not in current_url:
                                login_success = True
                                LOGGER.info("登录成功！")
                                break

                    if not login_success:
                        _update_login_progress("failed", "验证码等待超时（2分钟），请重试或使用新标签页登录")
                        raise ValueError("验证码等待超时（2分钟），请重试")
                    break
                else:
                    raise ValueError("检测到验证码，需要手动登录。请在前端页面点击刷新登录按钮完成验证。")

            error_selectors = [
                'tag:div@class:=error',
                'tag:span@class:=error',
                'tag:div@class:=login-error',
            ]
            for selector in error_selectors:
                try:
                    err_ele = page.ele(selector, timeout=0.5)
                    if err_ele and err_ele.text:
                        err_text = err_ele.text.strip()
                        if err_text:
                            raise ValueError(f"登录失败: {err_text}")
                except ValueError:
                    raise
                except Exception:
                    pass

        if not login_success:
            _update_login_progress("failed", f"登录超时（{max_wait}秒），可能需要手动处理验证码")
            raise ValueError(f"登录超时（{max_wait}秒），可能需要手动处理验证码")

        page.get('https://onetouch-partner.alibaba.com/ptnBase/luyou/express/list.htm')
        _time.sleep(5)

        browser_cookies = page.cookies()
        cookies = {}
        for cookie in browser_cookies:
            cookies[cookie.get('name')] = cookie.get('value', '')

        ctoken = get_ctoken_from_cookies(cookies)
        if ctoken:
            LOGGER.info(f"浏览器自动化登录成功，获取到ctoken: {ctoken[:20]}...")
            _save_login_cookies(cookies, ctoken)
        else:
            LOGGER.warning("浏览器自动化登录成功，但未获取到ctoken")

        return cookies

    except Exception as exc:
        LOGGER.error(f"浏览器自动化登录阿里巴巴失败: {str(exc)}")
        raise
    finally:
        if page and not use_remote:
            # 远程调试模式不关闭浏览器，只关闭连接
            try:
                page.quit()
            except Exception:
                pass


def _save_login_cookies(cookies: Dict[str, str], ctoken: str) -> None:
    """将登录获取的cookies保存到抓包文件中，以便后续使用"""
    tb_token = cookies.get('_tb_token_', '')
    
    cookie_lines = []
    for name, value in cookies.items():
        cookie_lines.append(f"cookie: {name}={value}")
    
    capture_content = f"""GET /ptnBase/luyou/express/order/list.json?_tb_token_={tb_token}&ctoken={ctoken}&json=%7B%22statusType%22%3A%22ALL%22%2C%22source%22%3Anull%2C%22generalSearchField%22%3A%22%22%2C%22queryNewList%22%3Atrue%2C%22serviceType%22%3Anull%2C%22statusList%22%3A%5B%5D%2C%22queryPayStatus%22%3Anull%2C%22exceptionOrderStatus%22%3Anull%2C%22currentPage%22%3A1%2C%22pageSize%22%3A10%2C%22sort%22%3A%7B%7D%2C%22needConsignorAddress%22%3Atrue%2C%22needConsigneeAddress%22%3Atrue%7D h2
host: onetouch-partner.alibaba.com
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0
accept: */*
accept-language: zh-CN,zh;q=0.9,en;q=0.8
referer: https://onetouch-partner.alibaba.com/ptnBase/luyou/express/list.htm
bx-v: 2.5.11
connection: keep-alive
{chr(10).join(cookie_lines)}
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
pragma: no-cache
cache-control: no-cache
"""
    
    capture_path = BASE_DIR / REQUEST_CAPTURE_FILE
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_text(capture_content, encoding='utf-8')
    LOGGER.info(f"登录凭证已保存到抓包文件: {capture_path}")


def get_ctoken_from_cookies(cookies: Dict[str, str]) -> Optional[str]:
    """从cookie中提取ctoken"""
    # 从xman_us_t中提取ctoken
    xman_us_t = cookies.get("xman_us_t", "")
    if xman_us_t:
        # 解析xman_us_t，提取ctoken
        parts = xman_us_t.split("&")
        for part in parts:
            if part.startswith("ctoken="):
                return part.split("=")[1]
    return None


def run_pipeline(order_number: str, use_fixture: bool, push_wedoc: bool, extra_fields: List[str], document: str = "gangqian", username: str = None, password: str = None) -> Dict[str, Any]:
    LOGGER.info(f"开始处理订单 {order_number}，获取方式: {'响应文件' if use_fixture else '实时接口'}，目标文档: {document}")
    
    try:
        payload = fetch_order_data_fixture(order_number) if use_fixture else fetch_order_data_live(order_number, username, password)
        LOGGER.info(f"成功获取订单 {order_number} 数据")

        record = find_order_record(payload, order_number)
        if not record:
            LOGGER.error(f"未查询到单号 {order_number} 的订单数据")
            raise ValueError(f"未查询到单号 {order_number} 的订单数据")

        extracted, missing_fields = extract_business_data(record)
        if missing_fields:
            LOGGER.warning("单号 %s 缺少必要字段: %s", order_number, ", ".join(missing_fields))

        schema_definition = build_schema_definition(extra_fields, document)
        values = build_wedoc_values(extracted, schema_definition)
        preview_payload = build_preview_payload(values, schema_definition)
        webhook_payload = build_webhook_payload(values)

        webhook_response = None
        if push_wedoc:
            LOGGER.info(f"开始同步订单 {order_number} 数据到 {document} 文档")
            webhook_response = send_to_wedoc(webhook_payload, document)
            LOGGER.info(f"成功同步订单 {order_number} 数据到 {document} 文档")

        LOGGER.info(f"订单 {order_number} 处理完成")
        return {
            "order_number": order_number,
            "missing_fields": missing_fields,
            "extracted": extracted,
            "preview_payload": preview_payload,
            "webhook_payload": webhook_payload,
            "webhook_response": webhook_response,
            "source_mode": "fixture" if use_fixture else "live",
            "document": document,
        }
    except Exception as exc:
        LOGGER.exception(f"处理订单 {order_number} 时发生错误")
        raise


@app.route("/", methods=["GET", "POST"])
def index() -> str:
    context: Dict[str, Any] = {
        "default_order_number": DEFAULT_ORDER_NUMBER,
        "request_capture_file": REQUEST_CAPTURE_FILE,
        "response_fixture_file": RESPONSE_FIXTURE_FILE,
        "results": None,
        "error": None,
    }

    if request.method == "POST":
        order_numbers_input = request.form.get("order_number", "").strip()
        use_fixture = request.form.get("use_fixture") == "on"
        push_wedoc = request.form.get("push_wedoc") == "on"
        extra_fields = split_extra_fields(request.form.get("extra_fields", "").strip())
        document = request.form.get("wedoc_document", "gangqian")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        try:
            order_numbers = [order.strip() for order in order_numbers_input.split('\n') if order.strip()]
            if not order_numbers:
                context["error"] = "请输入至少一个阿里单号"
                context["form_values"] = {
                    "order_number": order_numbers_input,
                    "use_fixture": use_fixture,
                    "push_wedoc": push_wedoc,
                    "extra_fields": request.form.get("extra_fields", "").strip(),
                    "wedoc_document": document,
                    "username": username,
                }
                return render_template("index.html", **context)
            results = []
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_order = {}
                for order_number in order_numbers:
                    future = executor.submit(run_pipeline, order_number, use_fixture, push_wedoc, extra_fields, document, username, password)
                    future_to_order[future] = order_number
                
                # 收集所有任务的结果
                for future in as_completed(future_to_order):
                    order_number = future_to_order[future]
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.exception("处理单号 %s 时失败", order_number)
                        # 将错误信息添加到结果中
                        results.append({
                            "order_number": order_number,
                            "error": str(exc),
                            "source_mode": "error",
                            "document": document
                        })
            context["results"] = results
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("批量处理时失败")
            context["error"] = str(exc)

        context["form_values"] = {
            "order_number": order_numbers_input,
            "use_fixture": use_fixture,
            "push_wedoc": push_wedoc,
            "extra_fields": request.form.get("extra_fields", "").strip(),
            "wedoc_document": document,
        }

    return render_template("index.html", **context)


@app.route("/dingtalk/webhook", methods=["POST"])
def dingtalk_webhook() -> str:
    try:
        data = request.get_json()
        if not data:
            data = {}
        
        raw_body = request.get_data().decode('utf-8')
        LOGGER.info(f"收到钉钉请求原始数据: {raw_body[:500]}")
        
        text = ""
        if isinstance(data, dict):
            text = data.get("text", {}).get("content", "").strip() if isinstance(data.get("text"), dict) else ""
            if not text:
                text = data.get("content", "").strip()
            if not text:
                text = data.get("msg", {}).get("content", "").strip() if isinstance(data.get("msg"), dict) else ""
        
        LOGGER.info(f"提取到消息内容: '{text}'")
        
        if not text:
            import re
            for pattern in [r'ALS\d{11,13}', r'\d{18,21}']:
                matches = re.findall(pattern, raw_body)
                if matches:
                    text = matches[0]
                    LOGGER.info(f"从原始数据中提取订单号: {text}")
                    break
        
        if not text:
            LOGGER.error("消息内容为空")
            return "请提供订单号，格式：@机器人 ALS12345678901 或 信保单号"
        
        text = text.replace("@ALS查单", "").strip()
        text = text.replace("@2222", "").strip()
        
        if isinstance(data, dict):
            at_users = data.get("at", {}).get("atMobiles", []) if isinstance(data.get("at"), dict) else []
            mentioned_list = data.get("at", {}).get("mentionedList", []) if isinstance(data.get("at"), dict) else []
            
            if isinstance(mentioned_list, list):
                for at_user in mentioned_list:
                    text = text.replace(f"@{at_user}", "").strip()
            
            if isinstance(at_users, list):
                for at_mobile in at_users:
                    text = text.replace(f"@{at_mobile}", "").strip()
        
        text = text.strip()
        LOGGER.info(f"清理后的文本: '{text}'")
        
        import re
        order_numbers = []
        for token in text.split():
            token = token.strip()
            if not token:
                continue
            if re.match(r'^ALS\d{11,13}$', token, re.IGNORECASE):
                order_numbers.append(token.upper())
            elif re.match(r'^\d{18,21}$', token):
                order_numbers.append(token)
        
        if not order_numbers:
            order_numbers = [token.strip() for token in text.split() if token.strip()]
        
        LOGGER.info(f"解析到的订单号列表: {order_numbers}")
        
        if not order_numbers:
            LOGGER.error("未解析到有效订单号")
            return "请提供有效的订单号，格式：ALS开头+11位数字 或 18-21位信保单号"
        
        results = []
        
        def query_order(order_number):
            try:
                order_number = str(order_number).strip()
                if order_number.upper().startswith("ALS"):
                    order_number = order_number.upper()
                LOGGER.info(f"开始查询订单: {order_number}")
                payload = fetch_order_data_live(order_number, wait_for_captcha=False)
                
                records = find_order_records(payload, order_number)
                if not records:
                    return f"订单 {order_number} 未查询到"
                
                record_results = []
                for idx, record in enumerate(records, 1):
                    extracted, _ = extract_business_data(record)
                    
                    logistics_info = "-"
                    return_type = ""
                    order_id = record.get("id") or record.get("orderId")
                    if not order_id and extracted.get("orderNumber", "").upper().startswith("ALS"):
                        try:
                            order_id = str(int(extracted.get("orderNumber", "")[3:]))
                        except (ValueError, IndexError):
                            order_id = None
                    
                    if order_id:
                        detail = fetch_order_detail(str(order_id))
                        if detail:
                            lcn = _extract_logistics_company_name(detail)
                            if lcn:
                                logistics_info = lcn
                            rt = _extract_return_type(detail)
                            if rt:
                                return_type = rt
                    
                    extracted["logisticsCompanyName"] = logistics_info
                    
                    trade_biz_id = extracted.get("tradeBizId", "")
                    
                    order_status_val = ensure_chinese(extracted.get("orderStatus", "-"), "orderStatus")
                    solution_name_val = ensure_chinese(extracted.get("solutionName", "-"), "solutionName")
                    logistics_info_val = ensure_chinese(logistics_info, "logisticsCompanyName")
                    return_type_val = ensure_chinese(return_type, "returnType") if return_type else ""
                    
                    result_text = f"订单 {order_number} 查询成功\n"
                    result_text += f"订单号：{extracted.get('orderNumber', '-')}\n"
                    result_text += f"下单账号：{extracted.get('sellerLoginId', '-')}\n"
                    result_text += f"用户名称：{extracted.get('customerName', '-')}\n"
                    result_text += f"电话：{extracted.get('mobileNo', '-')}\n"
                    result_text += f"邮箱：{extracted.get('email', '-')}\n"
                    result_text += f"类型：{solution_name_val}\n"
                    result_text += f"订单状态：{order_status_val}\n"
                    result_text += f"揽收信息：{logistics_info_val}\n"
                    result_text += f"发件地址：{extracted.get('senderAddress', '-')}"
                    if return_type_val:
                        result_text += f"\n退货方式：{return_type_val}"

                    if trade_biz_id and trade_biz_id != order_number:
                        result_text += f"\n信保单号：{trade_biz_id}"
                    
                    record_results.append(result_text)
                    LOGGER.info(f"订单 {order_number} 查询成功 (第{idx}条)")
                
                return "\n——————————————\n".join(record_results)
            except ValueError as exc:
                exc_str = str(exc)
                if "风控" in exc_str or "RGV587" in exc_str:
                    return f"⚠️ 订单 {order_number} 查询失败：登录被阿里风控拦截，请稍后重试或联系管理员"
                if "验证码" in exc_str or "登录" in exc_str:
                    return f"⚠️ 订单 {order_number} 查询失败：登录会话已过期，请稍后重试或联系管理员"
                return f"订单 {order_number} 查询失败: {exc_str[:50]}"
            except Exception as exc:
                error_msg = f"订单 {order_number} 查询失败: {str(exc)[:50]}"
                LOGGER.error(error_msg)
                return error_msg
        
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(query_order, order_number): order_number for order_number in order_numbers[:5]}
            for future in concurrent.futures.as_completed(futures, timeout=8):
                try:
                    results.append(future.result())
                except concurrent.futures.TimeoutError:
                    order_number = futures[future]
                    results.append(f"⚠️ 订单 {order_number} 查询超时，请稍后重试")
                    LOGGER.error(f"订单 {order_number} 查询超时")
                except Exception as exc:
                    order_number = futures[future]
                    results.append(f"订单 {order_number} 查询失败: {str(exc)[:50]}")
                    LOGGER.error(f"订单 {order_number} 查询异常: {exc}")
        
        response_text = "\n——————————————\n".join(results)
        LOGGER.info(f"准备发送的响应: {response_text[:200]}")
        
        return response_text
    
    except Exception as exc:
        error_msg = f"❌ 处理请求失败: {str(exc)[:50]}"
        LOGGER.exception("处理钉钉机器人请求失败")
        return error_msg


# ─── 会话状态 API ────────────────────────────────────────────────────────
@app.route("/api/session/status")
def api_session_status():
    """获取当前会话状态"""
    return json.dumps(session_mgr.get_status(), ensure_ascii=False)


@app.route('/favicon.ico')
def favicon():
    """返回网站图标"""
    # 简单的 1x1 透明像素 PNG 图标（base64 解码）
    import base64
    png_data = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==')
    return png_data, 200, {'Content-Type': 'image/png'}


@app.route("/api/session/refresh", methods=["POST"])
def api_session_refresh():
    """手动刷新登录凭证（后台执行）"""
    if session_mgr._is_logging_in:
        return json.dumps({
            "success": False,
            "message": "已有登录任务进行中",
            "status": session_mgr.get_status(),
        }, ensure_ascii=False)

    data = request.get_json(silent=True) or {}
    username = data.get("username") or request.form.get("username", "").strip()
    password = data.get("password") or request.form.get("password", "").strip()
    remote_debug_port = data.get("remote_debug_port") or request.form.get("remote_debug_port", "").strip()

    rdp = None
    if remote_debug_port:
        try:
            rdp = int(remote_debug_port)
        except ValueError:
            pass

    def _do_login():
        try:
            session_mgr.refresh_login(username, password, wait_for_captcha=True, remote_debug_port=rdp)
        except Exception as exc:
            LOGGER.error(f"后台登录任务异常: {exc}")
            _update_login_progress("failed", f"登录异常: {str(exc)[:100]}")

    thread = threading.Thread(target=_do_login, daemon=True)
    thread.start()

    return json.dumps({
        "success": True,
        "message": "登录任务已启动",
        "status": session_mgr.get_status(),
    }, ensure_ascii=False)


@app.route("/api/session/check")
def api_session_check():
    """检查会话是否有效"""
    valid = session_mgr.check_alive()
    return json.dumps({
        "is_valid": valid,
        "status": session_mgr.get_status(),
    }, ensure_ascii=False)


@app.route("/api/session/manual", methods=["POST"])
def api_session_manual():
    """手动输入凭证（ctoken和cookies），用于服务器环境自动登录失败时的备选方案"""
    data = request.get_json(silent=True) or {}
    ctoken = data.get("ctoken", "").strip()
    cookies_str = data.get("cookies", "").strip()

    if not ctoken:
        return json.dumps({"success": False, "message": "ctoken不能为空"}, ensure_ascii=False)

    cookies = {}
    if cookies_str:
        for item in cookies_str.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                cookies[name.strip()] = value.strip()

    # 构建 xman_us_t：如果提供了原始值则追加ctoken，否则仅使用ctoken
    xman_us_t = cookies.get("xman_us_t", "")
    if xman_us_t:
        # 移除旧的ctoken（如果存在），避免重复
        parts = [p for p in xman_us_t.split("&") if not p.startswith("ctoken=")]
        parts.append(f"ctoken={ctoken}")
        cookies["xman_us_t"] = "&".join(parts)
    else:
        cookies["xman_us_t"] = f"ctoken={ctoken}"

    # 如果没有提供 _tb_token_，生成一个随机值（部分接口需要）
    if "_tb_token_" not in cookies:
        import random, string
        cookies["_tb_token_"] = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        LOGGER.info("未提供 _tb_token_，已生成随机值")

    with session_mgr._lock:
        session_mgr._cookies = cookies
        session_mgr._ctoken = ctoken
        session_mgr._tb_token = cookies.get("_tb_token_", "")
        session_mgr._is_expired = False
        session_mgr._last_refresh = datetime.now()

    _save_login_cookies(cookies, ctoken)

    LOGGER.info(f"手动输入凭证成功，ctoken: {ctoken[:10]}...")
    return json.dumps({
        "success": True,
        "message": "凭证更新成功",
        "status": session_mgr.get_status(),
    }, ensure_ascii=False)


@app.route("/api/session/login-progress")
def api_session_login_progress():
    """获取当前登录进度"""
    return json.dumps(get_login_progress(), ensure_ascii=False)


@app.route("/api/session/login-screenshot")
def api_session_login_screenshot():
    """获取登录截图"""
    progress = get_login_progress()
    screenshot_path = progress.get("screenshot_path")
    if screenshot_path and os.path.exists(screenshot_path):
        from flask import send_file
        return send_file(screenshot_path, mimetype="image/png")
    return "", 404


@app.route("/api/session/submit-cookies", methods=["POST", "OPTIONS"])
def api_session_submit_cookies():
    """接收从浏览器新标签页提取的cookies（支持跨域）"""
    if request.method == "OPTIONS":
        resp = json.dumps({})
        return resp, 200, {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }

    data = request.get_json(silent=True) or {}
    cookies_str = data.get("cookies", "").strip()
    ctoken = data.get("ctoken", "").strip()

    if not ctoken and not cookies_str:
        return json.dumps({"success": False, "message": "未提供有效的凭证"}, ensure_ascii=False), 200, {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        }

    cookies = {}
    if cookies_str:
        for item in cookies_str.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                cookies[name.strip()] = value.strip()

    if not ctoken:
        ctoken = get_ctoken_from_cookies(cookies)

    if ctoken:
        xman_us_t = cookies.get("xman_us_t", "")
        if "ctoken=" not in xman_us_t:
            cookies["xman_us_t"] = xman_us_t + f"&ctoken={ctoken}" if xman_us_t else f"ctoken={ctoken}"

        with session_mgr._lock:
            if cookies:
                existing = session_mgr._cookies.copy()
                existing.update(cookies)
                session_mgr._cookies = existing
            else:
                session_mgr._cookies = {"xman_us_t": f"ctoken={ctoken}"}
            session_mgr._ctoken = ctoken
            session_mgr._tb_token = session_mgr._cookies.get("_tb_token_", "")
            session_mgr._is_expired = False
            session_mgr._last_refresh = datetime.now()

        _save_login_cookies(session_mgr._cookies, ctoken)
        _update_login_progress("success", "通过新标签页登录成功，凭证已更新")

        LOGGER.info(f"通过新标签页提交凭证成功，ctoken: {ctoken[:10]}...")
        return json.dumps({"success": True, "message": "凭证提交成功"}, ensure_ascii=False), 200, {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        }

    return json.dumps({"success": False, "message": "无法从cookies中提取ctoken"}, ensure_ascii=False), 200, {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
    }


if __name__ == "__main__":
    ensure_runtime_dirs()
    # 启动时从抓包文件加载凭证
    session_mgr.load_from_capture_file()
    # 启动 Keep-Alive 保活线程
    if APP_CONFIG.get("keep_alive", {}).get("enabled", True):
        session_mgr.start_keep_alive()
    app.run(host="0.0.0.0", port=3020, debug=True)
