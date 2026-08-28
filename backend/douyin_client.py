from __future__ import annotations

import asyncio
import json
import os
import random
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .abogus_signer import ABogusSigner


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# 收藏接口请求 UA：必须与出签会话的 UA 保持一致
SIGN_SESSION_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36"
)

# 出签专用 agent-browser 隔离会话：独立于 default 会话，避免其他工具把出签页导航走。
# 登录态（cookies + secsdk localStorage）持久化在状态文件中，会话/守护进程失活时
# 可自动重建恢复（懒重建 + 出签成功后节流刷新保鲜）。
SIGN_SESSION_NAME = os.environ.get("DOUYIN_SIGN_SESSION", "douyin-sign")
SIGN_STATE_FILE = Path(
    os.environ.get(
        "DOUYIN_SIGN_STATE_FILE",
        str(Path.home() / ".agent-browser" / "sessions" / "douyin-sign.json"),
    )
)
SIGN_RECOVERY_URL = "https://www.douyin.com/user/self?showTab=favorite_collection"


SIGN_ASSETS_FILE = Path(
    os.environ.get(
        "DOUYIN_SIGN_ASSETS_FILE",
        str(Path(__file__).resolve().parent.parent / "data" / "sign-assets.json"),
    )
)


def _load_sign_assets() -> dict:
    try:
        data = json.loads(SIGN_ASSETS_FILE.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_sign_assets(assets: dict) -> None:
    try:
        SIGN_ASSETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SIGN_ASSETS_FILE.write_text(
            json.dumps(assets, ensure_ascii=False, indent=2), "utf-8"
        )
    except OSError:
        pass  # 缓存写失败不阻断主流程


_ASSET_EXTRACT_JS = (
    "(async()=>{const out={};"
    "try{const s=JSON.parse(localStorage.getItem('SysInfo')||'{}');"
    "if(s&&s.webid)out.webid=String(s.webid);}catch(e){}"
    "if(!out.webid){try{for(let i=0;i<localStorage.length;i++){"
    "const k=localStorage.key(i);"
    "if(k&&k.indexOf('__tea_cache_tokens_')===0){"
    "const v=JSON.parse(localStorage.getItem(k)||'{}');"
    "if(v&&v.user_unique_id){out.webid=String(v.user_unique_id);break;}}}}"
    "catch(e){}}"
    "try{const m=document.cookie.match(/(?:^|;\\s*)sdk_source_info=([^;]+)/);"
    "if(m)out.account_sdk_source_info=decodeURIComponent(m[1]);}catch(e){}"
    "return JSON.stringify(out);})()"
)


async def extract_sign_assets_from_browser(timeout: float = 15.0) -> dict:
    """从出签浏览器会话提取个人资产（webid / account_sdk_source_info）。"""
    proc = await asyncio.create_subprocess_exec(
        "agent-browser", "eval", "--stdin",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "AGENT_BROWSER_SESSION": SIGN_SESSION_NAME},
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {}
    raw = stdout.decode("utf-8", "replace").strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def resolve_sign_asset(key: str, env_name: str) -> str:
    """解析出签资产：env > 缓存文件 > 出签浏览器提取（成功则回写缓存）。"""
    raw = os.environ.get(env_name, "").strip()
    if raw:
        return raw
    assets = _load_sign_assets()
    value = str(assets.get(key) or "").strip()
    if value:
        return value
    extracted = await extract_sign_assets_from_browser()
    value = str(extracted.get(key) or "").strip()
    if value:
        merged = dict(assets)
        merged.update(extracted)
        merged["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_sign_assets(merged)
    return value
# 收藏接口参数集（顺序敏感）。与 BASE_PARAMS（detail 接口用）相互独立，勿混用。
COLLECTION_BASE_PARAMS: dict[str, str] = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "publish_video_strategy_type": "2",
    "pc_client_type": "1",
    "pc_libra_divert": "Mac",
    "update_version_code": "170400",
    "support_h265": "1",
    "support_dash": "1",
    "version_code": "170400",
    "version_name": "17.4.0",
    "cookie_enabled": "true",
    "screen_width": "800",
    "screen_height": "600",
    "browser_language": "en-US",
    "browser_platform": "MacIntel",
    "browser_name": "Chrome Headless",
    "browser_version": "151.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "151.0.0.0",
    "os_name": "Mac OS",
    "os_version": "10.15.7",
    "cpu_core_num": "16",
    "device_memory": "32",
    "platform": "PC",
    "downlink": "1.7",
    "effective_type": "4g",
    "round_trip_time": "100",
}

BASE_PARAMS: dict[str, str] = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "pc_libra_divert": "Windows",
    "support_h265": "1",
    "support_dash": "1",
    "version_code": "170400",
    "version_name": "17.4.0",
    "cookie_enabled": "true",
    "screen_width": "1536",
    "screen_height": "864",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "139.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "139.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": "16",
    "device_memory": "8",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "200",
    "uifid": "",
    "msToken": "",
    "publish_video_strategy_type": "2",
}

QRCODE_PARAMS: dict[str, str] = {
    "service": "https://www.douyin.com",
    "need_logo": "false",
    "need_short_url": "true",
    "passport_jssdk_version": "1.0.22",
    "passport_jssdk_type": "pro",
    "aid": "6383",
    "language": "zh",
    "account_sdk_source": "sso",
    "passport_ztsdk": "0",
    "passport_verify": "1.0.14",
    "device_platform": "web_app",
    "msToken": "",
}

# account_sdk_source_info 运行时解析（环境变量 / 缓存文件 / 出签浏览器提取保鲜）


class AuthExpiredError(RuntimeError):
    pass


class DouyinRequestError(RuntimeError):
    pass


class SignSessionError(RuntimeError):
    """浏览器出签会话不可用：agent-browser 未运行或不在抖音登录页"""


class RiskControlBlockedError(DouyinRequestError):
    """请求被远端临时拒绝，等待后重试通常可恢复。"""


@dataclass(frozen=True)
class Settings:
    root: Path = Path(".")
    cookie_json: Path = Path("cookie.json")
    cookie_txt: Path = Path("cookie.txt")
    # 签名会话的 webid：与出签浏览器保持一致，留空时自动解析
    webid: str = ""
    # 收藏列表翻页间隔（秒，每页实际取区间内随机值），
    # 可用环境变量 DOUYIN_PAGE_INTERVAL_MIN / DOUYIN_PAGE_INTERVAL_MAX 覆盖
    page_interval_min: float = 6.0
    page_interval_max: float = 12.0

    @classmethod
    def from_root(cls, root: Path | str = ".") -> "Settings":
        base = Path(root).resolve()
        interval_min = _env_float("DOUYIN_PAGE_INTERVAL_MIN", 6.0)
        interval_max = _env_float("DOUYIN_PAGE_INTERVAL_MAX", 12.0)
        if interval_min < 0:
            interval_min = 0.0
        if interval_max < interval_min:
            interval_max = interval_min
        return cls(
            root=base,
            cookie_json=base / "cookie.json",
            cookie_txt=base / "cookie.txt",
            webid=os.environ.get("DOUYIN_WEBID", ""),
            page_interval_min=interval_min,
            page_interval_max=interval_max,
        )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def normalize_cookie(cookie: str) -> str:
    return cookie.strip().removeprefix("Cookie:").strip()


def cookie_to_dict(cookie: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in normalize_cookie(cookie).split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key:
            result[key] = value
    return result


def cookie_dict_to_str(cookie: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookie.items() if value is not None)


def cookie_value(cookie: str, name: str) -> str:
    return cookie_to_dict(cookie).get(name, "")


def read_cookie(settings: Settings) -> str:
    env_cookie = os.environ.get("DOUYIN_COOKIE", "").strip()
    if env_cookie:
        return normalize_cookie(env_cookie)

    if settings.cookie_json.exists():
        data = json.loads(settings.cookie_json.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("cookie"), str):
            return normalize_cookie(data["cookie"])
        if isinstance(data, dict):
            return cookie_dict_to_str({str(k): str(v) for k, v in data.items()})

    if settings.cookie_txt.exists():
        return normalize_cookie(settings.cookie_txt.read_text(encoding="utf-8"))

    raise AuthExpiredError("auth_expired")


def save_cookie(settings: Settings, cookie: str, *, source: str) -> None:
    cookie = normalize_cookie(cookie)
    payload = {
        "cookie": cookie,
        "cookie_dict": cookie_to_dict(cookie),
        "source": source,
    }
    settings.cookie_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def random_mstoken(length: int = 128) -> str:
    """生成随机 msToken 兜底串；不能包含 "="（编码后与服务端规范化 query 不一致）。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=length))


def signed_query(abogus: Any, params: dict[str, str], method: str = "POST") -> str:
    # 注意：不能用 safe="="，否则值中的 "="（如 base64 型 msToken）不转义，
    # 与服务端规范化后的 query 不一致
    query = urlencode(params, quote_via=quote)
    return f"{query}&a_bogus={abogus.get_value(query, method)}"


async def sign_url_via_browser(url: str, timeout: float = 20.0) -> str:
    """通过 agent-browser 会话内 window.use('webSignUrl') 给 URL 追加签名参数。

    在专用隔离会话（SIGN_SESSION_NAME）中执行；依赖该会话停留在 www.douyin.com
    登录态。会话失活时由 sign_url_with_recovery 自动重建。浏览器 cookie 会话
    需与后端使用的 cookie 同源。"""

    js = (
        "(async()=>{try{const fn=window.use&&window.use('webSignUrl');"
        "if(typeof fn!=='function')return 'ERR:webSignUrl-unavailable';"
        f"const o=await fn({json.dumps(url)});"
        "return ((o&&o.url)||o);}catch(e){return 'ERR:'+e.message;}})()"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "agent-browser", "eval", "--stdin",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "AGENT_BROWSER_SESSION": SIGN_SESSION_NAME},
        )
    except FileNotFoundError as exc:
        raise SignSessionError(
            "agent-browser 未安装或不在 PATH：请先安装 agent-browser CLI"
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(js.encode()), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise SignSessionError("浏览器出签超时：agent-browser 会话无响应") from exc
    out = stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not out:
        err = stderr.decode("utf-8", "replace").strip()
        raise SignSessionError(
            f"浏览器出签失败（agent-browser 未运行或不在抖音页）：{err[:120] or out[:120]}"
        )
    try:
        signed = json.loads(out)
    except json.JSONDecodeError:
        signed = out.strip('"')
    if not isinstance(signed, str) or not signed.startswith("http"):
        raise SignSessionError(f"浏览器出签返回异常：{str(signed)[:160]}")
    if signed.startswith("ERR:"):
        raise SignSessionError(
            f"浏览器出签失败：{signed[4:160]}（检查 agent-browser 是否停在 www.douyin.com 登录页）"
        )
    return signed


async def _ensure_sign_session(timeout: float = 60.0) -> None:
    """重建出签会话：daemon 不在会自动拉起，并用状态文件恢复抖音登录态。"""
    if not SIGN_STATE_FILE.exists():
        raise SignSessionError(
            f"出签状态文件不存在（{SIGN_STATE_FILE}）：请在已登录抖音页的浏览器上执行 "
            f"agent-browser state save {SIGN_STATE_FILE} 生成初始状态"
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            "agent-browser",
            "--session", SIGN_SESSION_NAME,
            "--state", str(SIGN_STATE_FILE),
            "open", SIGN_RECOVERY_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SignSessionError("agent-browser 未安装或不在 PATH") from exc
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise SignSessionError("出签会话重建超时：agent-browser open 无响应") from exc
    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace").strip()
        raise SignSessionError(f"出签会话重建失败：{err[:160]}")


_last_state_refresh = 0.0


async def _refresh_sign_state(throttle_sec: float = 1800.0, timeout: float = 30.0) -> None:
    """出签成功后节流回写状态文件（默认 30 分钟一次），保持 cookie/localStorage
    新鲜：抖音轮换 cookie 后，恢复文件仍可用。刷新失败不阻断主流程。"""
    global _last_state_refresh
    if time.monotonic() - _last_state_refresh < throttle_sec:
        return
    try:
        SIGN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "agent-browser",
            "--session", SIGN_SESSION_NAME,
            "state", "save", str(SIGN_STATE_FILE),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            _last_state_refresh = time.monotonic()
            try:
                extracted = await extract_sign_assets_from_browser()
                if extracted:
                    merged = _load_sign_assets()
                    merged.update(extracted)
                    merged["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    _save_sign_assets(merged)
            except Exception:
                pass  # 资产刷新失败下次再试
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        pass  # 下次出签成功后再试


async def sign_url_with_recovery(url: str, timeout: float = 20.0) -> str:
    """带自愈的出签：会话失活时自动重建（拉起 daemon + 恢复登录态）后重试一次。

    能自愈：agent-browser daemon 死亡、Chrome 崩溃、出签会话被关闭、机器重启后。
    不能自愈：抖音 sessionid 真过期（走 AuthExpiredError 人工重新导出 cookie）。"""
    try:
        signed = await sign_url_via_browser(url, timeout)
    except SignSessionError as first_err:
        print(f"[douyin] 出签会话失活（{str(first_err)[:120]}），自动重建…", flush=True)
        await _ensure_sign_session()
        await asyncio.sleep(4.0)  # 等页面 JS 初始化 webSignUrl
        signed = await sign_url_via_browser(url, timeout)  # 仍失败则向上抛
    await _refresh_sign_state()
    return signed


def item_summary(item: dict[str, Any]) -> dict[str, Any]:
    aweme_id = str(item.get("aweme_id") or item.get("id") or "")
    share_info = item.get("share_info") or {}
    video = item.get("video") or {}
    cover = video.get("cover") or {}
    author = item.get("author") or {}
    return {
        "aweme_id": aweme_id,
        "share_url": share_info.get("share_url") or f"https://www.douyin.com/video/{aweme_id}",
        "desc": item.get("desc") or "",
        "cover": (cover.get("url_list") or [None])[0],
        "create_time": item.get("create_time"),
        "author": author.get("nickname") or "",
    }


class DouyinClient:
    # 临时失败重试退避（秒）：先快后慢
    RISK_RETRY_DELAYS = (3, 6, 15, 30, 60)
    # 重试耗尽后进入冷却；期间新同步先等待再发起
    RISK_COOLDOWN_SECONDS = 600.0

    def __init__(self, settings: Settings):
        self.settings = settings
        self.abogus = ABogusSigner(USER_AGENT)
        # 收藏接口签名：必须用出签会话的 UA
        self.collection_abogus = ABogusSigner(SIGN_SESSION_UA)
        self._blocked_until = 0.0

    async def fetch_collection_page(
        self,
        client: httpx.AsyncClient,
        cookie: str,
        cursor: int,
        count: int,
    ) -> dict[str, Any]:
        """拉取一页收藏视频。query 参数顺序与请求头须与出签会话保持一致。"""
        uifid = cookie_value(cookie, "UIFID")
        if not uifid:
            raise AuthExpiredError("auth_expired: cookie 缺少 UIFID 字段")
        verify_fp = cookie_value(cookie, "s_v_web_id")

        params = dict(COLLECTION_BASE_PARAMS)
        webid = self.settings.webid or await resolve_sign_asset("webid", "DOUYIN_WEBID")
        if not webid:
            raise SignSessionError(
                "webid 无法解析：请设置 DOUYIN_WEBID，或确保出签浏览器会话可用于自动提取"
            )
        params["webid"] = webid
        params["uifid"] = uifid
        query = urlencode(params, quote_via=quote)
        a_bogus = self.collection_abogus.get_value(query, "GET")
        url = (
            "https://www.douyin.com/aweme/v1/web/aweme/listcollection/?"
            f"{query}&a_bogus={quote(a_bogus, safe='')}"
        )
        if verify_fp:
            quoted_fp = quote(verify_fp, safe="")
            url += f"&verifyFp={quoted_fp}&fp={quoted_fp}"

        # 浏览器会话内出签（带会话自愈）
        signed_url = await sign_url_with_recovery(url)

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Cookie": cookie,
            "Origin": "https://www.douyin.com",
            "Referer": "https://www.douyin.com/user/self?showTab=favorite_collection",
            "User-Agent": SIGN_SESSION_UA,
            "uifid": uifid,
            "x-secsdk-csrf-token": "DOWNGRADE",
        }
        response = await client.post(
            signed_url,
            content=f"count={count}&cursor={cursor}",
            headers=headers,
        )
        if response.status_code == 401:
            raise AuthExpiredError("auth_expired")
        if response.status_code == 403:
            # 403 是临时拦截，并非登录失效；通常等待后可恢复，
            # 这里抛出由上层退避重试
            raise RiskControlBlockedError(
                f"risk_control: {response.text[:120]}"
            )
        response.raise_for_status()
        # 200 + 纯文本 "blocked"：软拦截
        if response.text.strip() == "blocked":
            raise RiskControlBlockedError("risk_control: blocked (soft)")
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise AuthExpiredError("auth_expired") from exc

    async def _fetch_page_with_retry(
        self,
        client: httpx.AsyncClient,
        cookie: str,
        cursor: int,
        count: int,
        max_retries: int = 5,
    ) -> dict[str, Any]:
        """翻页请求；临时拦截时指数退避重试，耗尽后进入冷却。

        max_retries=0 时单发不重试。"""
        delays = list(self.RISK_RETRY_DELAYS[: max(0, max_retries)])
        for attempt in range(len(delays) + 1):
            try:
                return await self.fetch_collection_page(client, cookie, cursor, count)
            except RiskControlBlockedError:
                if attempt >= len(delays) - 1:
                    # 重试耗尽：标记惩罚期，后续同步先冷静再发起请求
                    self._blocked_until = time.monotonic() + self.RISK_COOLDOWN_SECONDS
                    raise
                delay = delays[attempt] * random.uniform(0.8, 1.2)  # 抖动防共振
                await asyncio.sleep(delay)
        raise DouyinRequestError("unreachable")

    async def list_new_favorites(
        self,
        known_ids: set[str],
        *,
        max_items: int = 50,
        page_size: int = 10,
        max_pages: int = 20,
        max_retries: int = 5,
        respect_cooldown: bool = True,
    ) -> list[dict[str, Any]]:
        cookie = read_cookie(self.settings)
        cursor = 0
        items: list[dict[str, Any]] = []
        seen: set[str] = set()

        # 上次同步被拦截时先等待冷却期（上限 90s）
        cooldown = self._blocked_until - time.monotonic()
        if cooldown > 0:
            await asyncio.sleep(min(cooldown, 90.0))

        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for page in range(max_pages):
                if page > 0:
                    # 翻页间隔默认 6~12s（每页取随机值），
                    # 可用环境变量 DOUYIN_PAGE_INTERVAL_MIN/MAX 调整
                    await asyncio.sleep(
                        random.uniform(
                            self.settings.page_interval_min,
                            self.settings.page_interval_max,
                        )
                    )

                data = await self._fetch_page_with_retry(client, cookie, cursor, page_size, max_retries=max_retries)
                if data.get("status_code") not in (None, 0):
                    raise DouyinRequestError(json.dumps(data, ensure_ascii=False))

                raw_items = data.get("aweme_list") or []
                if not raw_items:
                    break

                for raw in raw_items:
                    summary = item_summary(raw)
                    aweme_id = summary["aweme_id"]
                    if not aweme_id or aweme_id in seen:
                        continue
                    if aweme_id in known_ids:
                        return items
                    seen.add(aweme_id)
                    items.append(summary)
                    if len(items) >= max_items:
                        return items

                cursor = int(data.get("cursor") or 0)
                if not data.get("has_more"):
                    break

        return items

    async def create_qrcode_session(self) -> dict[str, str]:
        params = QRCODE_PARAMS.copy()
        sdk_info = await resolve_sign_asset(
            "account_sdk_source_info", "DOUYIN_ACCOUNT_SDK_SOURCE_INFO"
        )
        if sdk_info:
            params["account_sdk_source_info"] = sdk_info
        query = urlencode(params, quote_via=quote)
        a_bogus = quote(self.abogus.get_value(query), safe="")
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "*/*",
            "Referer": "https://www.douyin.com/?recommend=1",
            "User-Agent": USER_AGENT,
        }
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                "https://sso.douyin.com/get_qrcode/",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                preview = response.text[:300].replace("\n", "\\n")
                raise DouyinRequestError(
                    f"qrcode endpoint returned non-JSON HTTP {response.status_code}: {preview}"
                ) from exc
        try:
            return {
                "qr_url": data["data"]["qrcode_index_url"],
                "session_token": data["data"]["token"],
            }
        except KeyError as exc:
            raise DouyinRequestError(json.dumps(data, ensure_ascii=False)) from exc

    async def check_qrcode_session(self, session_token: str) -> dict[str, str]:
        params = QRCODE_PARAMS.copy()
        sdk_info = await resolve_sign_asset(
            "account_sdk_source_info", "DOUYIN_ACCOUNT_SDK_SOURCE_INFO"
        )
        if sdk_info:
            params["account_sdk_source_info"] = sdk_info
        params["token"] = session_token
        params["is_frontier"] = "false"
        query = urlencode(params, quote_via=quote)
        a_bogus = quote(self.abogus.get_value(query), safe="")
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "*/*",
            "Referer": "https://www.douyin.com/?recommend=1",
            "User-Agent": USER_AGENT,
        }
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(
                f"https://sso.douyin.com/check_qrconnect/?{query}&a_bogus={a_bogus}",
                headers=headers,
            )
            response.raise_for_status()
            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                preview = response.text[:300].replace("\n", "\\n")
                raise DouyinRequestError(
                    f"qrcode status endpoint returned non-JSON HTTP {response.status_code}: {preview}"
                ) from exc

            if data.get("error_code"):
                raise DouyinRequestError(json.dumps(data, ensure_ascii=False))

            state_data = data.get("data") or {}
            status = str(state_data.get("status") or "")
            if status == "3":
                redirect_url = state_data.get("redirect_url")
                partial_cookie = response.headers.get("set-cookie", "")
                cookie = await self._complete_login_cookie(client, redirect_url, partial_cookie)
                save_cookie(self.settings, cookie, source="qrcode")
                return {"state": "confirmed"}
            if status in {"4", "5"}:
                return {"state": "expired"}
            return {"state": "pending"}

    async def _complete_login_cookie(
        self,
        client: httpx.AsyncClient,
        redirect_url: str,
        partial_cookie: str,
    ) -> str:
        if not redirect_url:
            raise DouyinRequestError("missing redirect_url")
        headers = {
            "Cookie": cookie_set_header_to_cookie(partial_cookie),
            "User-Agent": USER_AGENT,
            "Referer": "https://www.douyin.com/?recommend=1",
        }
        response = await client.get(redirect_url, headers=headers, follow_redirects=True)
        cookies: dict[str, str] = {}
        cookies.update(cookie_to_dict(cookie_set_header_to_cookie(partial_cookie)))
        for item in response.cookies.jar:
            cookies[item.name] = item.value
        set_cookie = response.headers.get("set-cookie", "")
        cookies.update(cookie_to_dict(cookie_set_header_to_cookie(set_cookie)))
        cookie = cookie_dict_to_str(cookies)
        if not cookie:
            raise DouyinRequestError("login confirmed but no cookie returned")
        return cookie


def cookie_set_header_to_cookie(set_cookie: str) -> str:
    if not set_cookie:
        return ""
    parts: list[str] = []
    for chunk in set_cookie.split(", "):
        first = chunk.split(";", 1)[0].strip()
        if "=" in first:
            parts.append(first)
    return "; ".join(parts)
