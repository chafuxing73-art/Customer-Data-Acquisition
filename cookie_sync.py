"""
本地 Cookie 自动同步脚本

功能：自动从本地 Chrome 浏览器读取阿里巴巴的 cookies，并同步到远程服务器。
使用方式：
    python cookie_sync.py                    # 同步一次
    python cookie_sync.py --interval 30      # 每30分钟自动同步一次
    python cookie_sync.py --server http://192.168.24.29:3020   # 指定服务器地址

依赖安装：
    pip install browser_cookie3 requests

注意：
    - 运行前需要先在 Chrome 中登录阿里巴巴后台
    - Chrome 浏览器需要关闭（browser_cookie3 无法读取正在运行的 Chrome 的 cookies）
    - 如果 Chrome 正在运行，脚本会尝试使用 --copy 方式（需手动操作）
"""

import argparse
import json
import re
import sys
import time
import requests


SERVER_URL = "http://192.168.24.29:3020"
ALIBABA_DOMAIN = ".alibaba.com"


def get_cookies_from_chrome():
    try:
        import browser_cookie3
    except ImportError:
        print("错误：需要安装 browser_cookie3 库")
        print("请运行：pip install browser_cookie3")
        return None

    try:
        cj = browser_cookie3.chrome(domain_name=ALIBABA_DOMAIN)
    except Exception as e:
        print(f"读取 Chrome cookies 失败: {e}")
        print("提示：请确保 Chrome 浏览器已关闭")
        return None

    cookies = {}
    ctoken = None
    for cookie in cj:
        cookies[cookie.name] = cookie.value
        if cookie.name == "xman_us_t":
            m = re.search(r"ctoken=([^&]+)", cookie.value)
            if m:
                ctoken = m.group(1)

    return cookies, ctoken


def get_cookies_manual():
    print("\n=== 手动输入模式 ===")
    print("1. 在 Chrome 中登录 https://onetouch-partner.alibaba.com")
    print("2. 按 F12 → Application → Cookies")
    print("3. 复制 xman_us_t 的值")
    print()
    xman_us_t = input("请粘贴 xman_us_t 的值: ").strip()
    if not xman_us_t:
        print("输入为空，退出")
        return None

    m = re.search(r"ctoken=([^&]+)", xman_us_t)
    ctoken = m.group(1) if m else None

    cookies = {"xman_us_t": xman_us_t}
    tb_token_m = re.search(r"_tb_token_=([^&;]+)", xman_us_t)
    if not tb_token_m:
        tb_token = input("请粘贴 _tb_token_ 的值（可选，直接回车跳过）: ").strip()
        if tb_token:
            cookies["_tb_token_"] = tb_token

    return cookies, ctoken


def sync_cookies(server_url, cookies, ctoken):
    if not ctoken:
        print("错误：未能从 cookies 中提取 ctoken")
        return False

    cookies_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    try:
        resp = requests.post(
            f"{server_url}/api/session/submit-cookies",
            json={"cookies": cookies_str, "ctoken": ctoken},
            timeout=10,
        )
        result = resp.json()
        if result.get("success"):
            print(f"同步成功！ctoken: {ctoken[:10]}...")
            return True
        else:
            print(f"同步失败：{result.get('message', '未知错误')}")
            return False
    except Exception as e:
        print(f"同步请求失败：{e}")
        return False


def check_server_status(server_url):
    try:
        resp = requests.get(f"{server_url}/api/session/status", timeout=5)
        data = resp.json()
        valid = data.get("is_valid", False)
        ctoken = data.get("ctoken", "")
        refresh = data.get("last_refresh", "")
        status = "有效" if valid else "已过期"
        print(f"服务器会话状态: {status} | ctoken: {ctoken[:10] if ctoken else '无'}... | 上次刷新: {refresh}")
        return valid
    except Exception as e:
        print(f"无法连接服务器：{e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="阿里巴巴 Cookie 自动同步工具")
    parser.add_argument("--server", default=SERVER_URL, help=f"服务器地址（默认：{SERVER_URL}）")
    parser.add_argument("--interval", type=int, default=0, help="自动同步间隔（分钟），0=仅同步一次")
    parser.add_argument("--manual", action="store_true", help="手动输入模式（不读取 Chrome）")
    args = parser.parse_args()

    print(f"服务器地址: {args.server}")
    print()

    def do_sync():
        if args.manual:
            result = get_cookies_manual()
        else:
            result = get_cookies_from_chrome()
            if result is None:
                print("\n自动读取失败，切换到手动输入模式...")
                result = get_cookies_manual()

        if result is None:
            return False

        cookies, ctoken = result
        if not ctoken:
            print("未能提取 ctoken，请确认已登录阿里巴巴")
            return False

        return sync_cookies(args.server, cookies, ctoken)

    if args.interval > 0:
        print(f"自动同步模式：每 {args.interval} 分钟同步一次")
        print("按 Ctrl+C 停止\n")
        while True:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始同步...")
            do_sync()
            check_server_status(args.server)
            print(f"下次同步时间：{args.interval} 分钟后\n")
            try:
                time.sleep(args.interval * 60)
            except KeyboardInterrupt:
                print("\n已停止")
                break
    else:
        do_sync()
        print()
        check_server_status(args.server)


if __name__ == "__main__":
    main()
