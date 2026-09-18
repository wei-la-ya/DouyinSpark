"""基于 Playwright + 真实浏览器的私信发送（绕开 imapi 8611 风控）。

移植自 bling-yshs/douyin-auto-spark@c42291a 的 main.ts：
  - 启动 Chromium（headless 或有头）
  - 注入 cookie
  - 访问 https://www.douyin.com/chat
  - 等待 chat 加载
  - 用搜索框找目标会话
  - 点"发私信"
  - 在编辑器输入消息
  - 按 Enter 发送

浏览器 JS bundle 自动处理 token / a_bogus / 签名，所以不会被 8611 拦截。
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import List, Optional

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
)


def _cookies_to_playwright(cookies: List[dict], base_url: str = "https://www.douyin.com") -> List[dict]:
    """Convert our Cookie-Editor-style cookies to Playwright addCookies format."""
    out = []
    for c in cookies:
        if not c.get("name") or c.get("value") is None:
            continue
        domain = c.get("domain") or ".douyin.com"
        # Playwright 接受 leading dot
        out.append({
            "name": c["name"],
            "value": str(c["value"]),
            "domain": domain,
            "path": c.get("path") or "/",
        })
    return out


async def send_via_browser(
    cookies: List[dict],
    targets: List[dict],
    headless: bool = True,
    user_data_dir: Optional[str] = None,
    browser_path: Optional[str] = None,
    settle_seconds: int = 10,
) -> dict:
    """通过真实 Chromium 浏览器发送私信。

    Args:
        cookies: Cookie-Editor 风格的 cookie 列表
        targets: 目标列表，每项至少含 sec_uid + nickname/unique_id
        headless: 是否无头模式
        user_data_dir: 浏览器用户目录（保存登录态等，可选）
        browser_path: 浏览器可执行文件路径（可选，默认 Chromium）
        settle_seconds: 加载后等待秒数

    Returns: {"sent": int, "skipped": [], "failures": [...]}
    """
    result = {"sent": 0, "skipped": [], "failures": []}

    pw: Playwright = await async_playwright().start()
    try:
        launch_kwargs = {"headless": headless}
        if browser_path:
            launch_kwargs["executablePath"] = browser_path
        if user_data_dir:
            launch_kwargs["user_data_dir"] = user_data_dir

        browser = await pw.chromium.launch(**launch_kwargs)

        context_kwargs = {}
        context = await browser.new_context(**context_kwargs)

        # 注入 cookie
        await context.add_cookies(_cookies_to_playwright(cookies))

        page = await context.new_page()
        try:
            await page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded")
        except Exception as e:
            await browser.close()
            await pw.stop()
            return {"sent": 0, "skipped": [], "failures": [{"message": f"goto failed: {e}"}]}

        # 等待页面加载（包括可能的滑动验证）
        await asyncio.sleep(settle_seconds)

        # 调试：截图 + HTML 摘要
        try:
            await page.screenshot(path="D:/code/douyin-spark-login/_chat_after_load.png", full_page=True)
            title = await page.title()
            url = page.url
            html_snippet = (await page.content())[:1500]
            print(f'[debug] title={title!r} url={url}')
            print(f'[debug] html (first 1500):')
            print(html_snippet)
        except Exception as e:
            print(f'[debug] screenshot failed: {e}')

        # 找搜索框
        try:
            search_input = page.locator('input.semi-input[placeholder="搜索"]').first
            await search_input.wait_for(state="visible", timeout=10000)
        except PWTimeout:
            await browser.close()
            await pw.stop()
            return {"sent": 0, "skipped": [], "failures": [{"message": "search input not found (may need verification)"}]}

        for target in targets:
            nick = (target.get("nickname") or target.get("unique_id") or target.get("sec_uid", "")[:12]).strip()
            if not nick:
                result["failures"].append({"name": "?", "message": "no nickname"})
                continue

            try:
                # 清空 + 输入
                await search_input.fill("")
                await search_input.fill(nick)
                await asyncio.sleep(1)

                # 找搜索结果里包含 nick 的项 + "发私信"按钮
                search_result = page.locator('.SearchPanelitembox').filter(
                    has=page.get_by_text(nick, exact=True)
                ).first

                visible = await search_result.is_visible(timeout=5000).catch(PWTimeout, False)
                if not visible:
                    result["skipped"].append(nick)
                    continue

                send_btn = search_result.get_by_text("发私信", exact=True)
                await send_btn.click(timeout=5000)

                # 编辑器
                editor = page.locator(
                    '.messageEditorimChatEditorContainer [data-slate-editor="true"][contenteditable="true"]'
                ).first
                await editor.wait_for(state="visible", timeout=10000)
                await editor.click()
                # 触发 typing（不能用 fill，必须 simulate 真实输入）
                await page.keyboard.insert_text(target.get("text", "续火🔥"))
                await asyncio.sleep(0.5)
                await page.keyboard.press("Enter")
                await asyncio.sleep(1)

                result["sent"] += 1
            except Exception as e:
                result["failures"].append({"name": nick, "message": str(e)})

        await browser.close()
    finally:
        await pw.stop()

    return result