#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
 TRAE 每日签到 —— GitHub Actions 云端版
-----------------------------------------------------------------------------
 为什么要有云端版：
   本机定时任务在「关机 / 休眠 / 断电 / 断网」时必然漏签。GitHub Actions
   跑在云端，与本机是否开机完全无关 —— 这是解决关机漏签的根本手段。

 签到链路（与本机版完全一致）：
   ① 会话 Cookie X-Cloudide-Session（Secret: TRAE_SESSION，约 14 天有效）
   ② POST /cloudide/api/v3/common/GetUserToken     换取全新短期 JWT
   ③ POST /trae/api/v2/ug/checkin_credits/status   查询今日是否已签到
   ④ POST /trae/api/v2/ug/checkin_credits/claim    领取签到积分
        带 Authorization: Cloud-IDE-JWT <jwt> / X-User-Region: cn / x-device-id

 幂等性：先查状态再决定要不要 claim，双端（本机 + 云端）同时开也不会重复领取。

 环境变量（GitHub Actions 中通过 Settings > Secrets 配置）：
   TRAE_SESSION        （必填）账号 1 的 X-Cloudide-Session
   TRAE_DEVICE_ID      （选填）16 位数字设备号，缺省随机
   TRAE_SESSION_2..N   （选填）第 N 个账号的会话，缺失即停止读取更多账号
   TRAE_DEVICE_ID_2..N （选填）对应设备号
   FEISHU_WEBHOOK      （选填）签到结果汇总推送

 退出码：0 = 全部成功；1 = 有账号失败（Actions 会标红）
=============================================================================
"""

import datetime
import hashlib
import json
import os
import random
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.trae.cn"
UA = "TraeCheckin-Cloud/1.0"

PATH_TOKEN = "/cloudide/api/v3/common/GetUserToken"
PATH_STATUS = "/trae/api/v2/ug/checkin_credits/status"
PATH_CLAIM = "/trae/api/v2/ug/checkin_credits/claim"

CODE_AUTH_INVALID = {1001, 401, 10001, 10002}
CODE_SUCCESS = {0, 200}
CODE_BUSY = {9074, 429}

HTTP_TIMEOUT = 30
BUSY_RETRIES = 3           # 换取 JWT / 查状态的重试次数（轻量接口）
BUSY_WAIT_MIN = 15         # 每次静置 15~30 秒后重试
BUSY_WAIT_MAX = 30

# 领取积分（claim）是风控重点：9074 会持续封锁十几分钟以上，
# 密集重试（15~30s 一次）实测完全无效，改为分钟级退避。
CLAIM_RETRIES = 4
CLAIM_BACKOFF = [60, 120, 240, 360]   # 秒，最坏情况约 13 分钟


def http_post(path, headers, body="{}"):
    """发起一次 POST 请求，返回 (http_status, dict_body, raw_text)。"""
    data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, {}, raw
    except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, OSError) as e:
        raise RuntimeError(f"网络异常：{type(e).__name__}: {e}")
    try:
        return 200, (json.loads(raw) if raw else {}), raw
    except json.JSONDecodeError:
        return 200, {}, raw


def post_with_retry(path, headers, body="{}", label=""):
    """带限流重试的 POST：遇到 9074/429 随机静置后重试。"""
    for attempt in range(1, BUSY_RETRIES + 1):
        status, data, raw = http_post(path, headers, body)
        code = data.get("code")
        if code in CODE_BUSY or status == 429:
            if attempt >= BUSY_RETRIES:
                raise RuntimeError(f"{label} 连续 {BUSY_RETRIES} 次遭遇限流，请稍后重试")
            wait = random.randint(BUSY_WAIT_MIN, BUSY_WAIT_MAX)
            print(f"  [限流] {label} code={code or status}，第 {attempt}/{BUSY_RETRIES} 次，{wait}s 后重试")
            time.sleep(wait)
            continue
        return status, data, raw
    raise RuntimeError(f"{label} 调用失败")


def get_token(session: str) -> str:
    """用 X-Cloudide-Session 换取全新 JWT。"""
    headers = {
        "Cookie": "X-Cloudide-Session=" + session,
        "Referer": "https://www.trae.cn/",
        "Origin": "https://www.trae.cn",
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
    }
    _, data, raw = post_with_retry(PATH_TOKEN, headers, "", "GetUserToken")
    token = (data.get("Result") or {}).get("Token") or data.get("token")
    if not token:
        raise RuntimeError(f"换取 JWT 失败：code={data.get('code')} msg={data.get('message') or raw[:160]}")
    return token


def stable_device_id(session: str) -> str:
    """从会话派生一个**稳定不变**的 16 位设备号。

    刻意不用随机数：真实客户端的设备号是固定的，每次运行都换一个新号
    更像脚本行为，容易被风控（code=9074）拦下。
    """
    h = int(hashlib.sha256(("device:" + session).encode()).hexdigest(), 16)
    lo, hi = 10 ** 15, 10 ** 16 - 1
    return str(lo + h % (hi - lo + 1))


def stable_machine_id(session: str) -> str:
    """派生稳定的 64 位十六进制机器号，对齐 trae 客户端的 X-Machine-Id。"""
    return hashlib.sha256(("machine:" + session).encode()).hexdigest()


def sign_headers(token: str, device_id: str, machine_id: str = "") -> dict:
    headers = {
        "Authorization": "Cloud-IDE-JWT " + token,
        "X-User-Region": "cn",
        "x-device-id": device_id,
        "Content-Type": "application/json",
        "User-Agent": UA,
        # 本机版（实测可成功领取）会带上这三个头。纯脚本请求缺了
        # Referer / Origin / X-Machine-Id，更容易被风控判为异常。
        "Referer": "https://www.trae.cn/",
        "Origin": "https://www.trae.cn",
    }
    if machine_id:
        headers["X-Machine-Id"] = machine_id
    return headers


def today_status(token: str, device_id: str, machine_id: str = "") -> dict:
    """查询今日签到状态。"""
    _, data, raw = post_with_retry(PATH_STATUS, sign_headers(token, device_id, machine_id),
                                   "{}", "状态查询")
    if not data:
        raise RuntimeError(f"状态查询无有效响应：{raw[:160]}")
    return data


def claim(token: str, device_id: str, machine_id: str = "") -> dict:
    """领取签到积分（单次，不重试 —— 重试策略交给 claim_with_backoff）。"""
    _, data, raw = http_post(PATH_CLAIM, sign_headers(token, device_id, machine_id), "{}")
    if not data:
        raise RuntimeError(f"签到无有效响应：{raw[:160]}")
    return data


def claim_with_backoff(session: str, device_id: str, machine_id: str = "") -> dict:
    """带长间隔退避的领取。

    code=9074 不是普通限流：实测会连续封锁十几分钟甚至更久，密集重试
    （每 15~30 秒一次）不仅无效，反而可能延长封锁。因此改为
    「少量尝试 + 分钟级退避」，且每次尝试都重新换取 JWT，避免复用
    同一个 token 被判为重复请求。
    """
    result: dict = {}
    for attempt in range(1, CLAIM_RETRIES + 2):
        token = get_token(session)                     # 每次都用全新 JWT
        result = claim(token, device_id, machine_id)
        code = result.get("code")
        if code in CODE_SUCCESS or code not in CODE_BUSY:
            return result                              # 成功或非限流错误，交由调用方判断
        if attempt > CLAIM_RETRIES:
            break
        wait = CLAIM_BACKOFF[min(attempt - 1, len(CLAIM_BACKOFF) - 1)]
        print(f"  [风控] 领取被拒 code={code}（{result.get('message', '')}），"
              f"第 {attempt}/{CLAIM_RETRIES + 1} 次，{wait}s 后换新 JWT 重试")
        time.sleep(wait)
    return result


def notify_feishu(webhook: str, text: str):
    if not webhook:
        return
    try:
        payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
        req = urllib.request.Request(webhook, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  [推送] 飞书通知已发送 HTTP {resp.status}")
    except Exception as e:
        print(f"  [推送] 飞书通知失败：{e}")


def beijing_now_str() -> str:
    """GitHub Actions 运行在 UTC，需 +8 小时换算成北京时间。"""
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    return (utc_now + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def iter_accounts():
    """产出 (序号, session, device_id)：账号 1 读 TRAE_SESSION，其后 TRAE_SESSION_N。"""
    s = os.environ.get("TRAE_SESSION", "").strip()
    if s:
        yield 1, s, os.environ.get("TRAE_DEVICE_ID", "").strip()
    n = 2
    while True:
        s = os.environ.get(f"TRAE_SESSION_{n}", "").strip()
        if not s:
            break
        yield n, s, os.environ.get(f"TRAE_DEVICE_ID_{n}", "").strip()
        n += 1


def random_device_id() -> str:
    return str(random.randint(10 ** 15, 10 ** 16 - 1))


def main() -> int:
    accounts = list(iter_accounts())
    if not accounts:
        print("错误：缺少环境变量 TRAE_SESSION")
        return 1

    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    ok_names, fail_names = [], []
    all_ok = True

    for index, session, device_id in accounts:
        name = f"账号 {index}"
        # 设备号固定（由会话派生），不再每次随机 —— 随机设备号更像脚本，易触发风控
        device_id = device_id or stable_device_id(session)
        machine_id = stable_machine_id(session)
        print(f"[{name}] device_id={device_id}")
        try:
            token = get_token(session)
            print(f"[{name}] 已换取新 JWT，长度={len(token)}")

            status = today_status(token, device_id, machine_id)
            code = status.get("code")
            if code in CODE_AUTH_INVALID:
                raise RuntimeError(f"会话已失效（code={code}），请重新获取 X-Cloudide-Session 并更新 Secret")
            if code is not None and code not in CODE_SUCCESS:
                raise RuntimeError(f"状态查询失败 code={code}：{status.get('message', '')}")

            if status.get("checked_in") or status.get("checked"):
                streak = status.get("continuous_days") or status.get("streak")
                extra = f"，连续签到 {streak} 天" if streak else ""
                print(f"[{name}] 今日已签到，无需重复领取{extra}")
                ok_names.append(name)
                continue
            if status.get("enable") is False:
                print(f"[{name}] 签到活动当前未开启（enable=false），跳过")
                ok_names.append(name)
                continue

            expect = status.get("credits") or status.get("today_credits") or status.get("reward_credits")
            if expect:
                print(f"[{name}] 今日待领取：{expect} 积分")

            result = claim_with_backoff(session, device_id, machine_id)
            rcode = result.get("code")
            if rcode in CODE_AUTH_INVALID:
                raise RuntimeError(f"签到被拒：会话失效（code={rcode}）")
            if rcode is not None and rcode not in CODE_SUCCESS:
                raise RuntimeError(f"签到失败 code={rcode}：{result.get('message', '')}")

            credits = result.get("credits") or result.get("granted_credits") or result.get("amount")
            streak = result.get("continuous_days") or result.get("streak") or result.get("checkin_days")
            tail = f"，本次获得 {credits} 积分" if credits else ""
            tail += f"，连续签到 {streak} 天" if streak else ""
            print(f"[{name}] 签到成功{tail}")
            ok_names.append(name)

        except Exception as e:
            print(f"[{name}] 异常：{e}")
            fail_names.append(name)
            all_ok = False

    summary = ["Trae 每日签到结果", f"时间：{beijing_now_str()}（北京时间）"]
    if ok_names:
        summary.append("成功：" + "、".join(ok_names))
    if fail_names:
        summary.append("失败：" + "、".join(fail_names) + "（请检查 Secret 是否过期）")
    notify_feishu(webhook, "\n".join(summary))

    print("全部账号签到完成" if all_ok else "存在失败账号")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
