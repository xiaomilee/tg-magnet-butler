"""把面板在手机/桌面两个尺寸下各拍几张，肉眼核对布局。只在本地预览用。"""
import asyncio
import os
import sys
from playwright.async_api import async_playwright

BASE = os.environ.get("TMB_UI_BASE", "http://127.0.0.1:8470/")
OUT = sys.argv[1] if len(sys.argv) > 1 else "./shot"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        # ---- 手机：iPhone 13 尺寸 ----
        phone = await browser.new_context(viewport={"width": 390, "height": 844},
                                          device_scale_factor=2, is_mobile=True,
                                          has_touch=True,
                                          user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 "
                                                      "like Mac OS X) AppleWebKit/605.1.15 "
                                                      "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"))
        pg = await phone.new_page()
        await pg.goto(BASE, wait_until="networkidle")
        await pg.wait_for_timeout(600)
        await pg.screenshot(path=OUT + "-m1-overview.png")

        # 抽屉
        await pg.click("#navToggle")
        await pg.wait_for_timeout(420)
        await pg.screenshot(path=OUT + "-m2-drawer.png")
        await pg.mouse.click(370, 420)          # 点抽屉右边的空白处收起（抽屉自己盖住了遮罩中心）
        await pg.wait_for_timeout(380)

        # 概览往下滚一屏：顶栏应该跟着页面一起滚上去（不再吸顶）
        await pg.evaluate("window.scrollTo(0, 620)")
        await pg.wait_for_timeout(420)
        await pg.screenshot(path=OUT + "-m3-overview-scrolled.png")
        await pg.evaluate("window.scrollTo(0, 0)")
        await pg.wait_for_timeout(300)

        # 最近投递：点「清除」之后的样子（只清屏，记录还在）
        await pg.locator("#btnHistClear").scroll_into_view_if_needed()
        await pg.wait_for_timeout(350)
        await pg.click("#btnHistClear")
        await pg.wait_for_timeout(2600)          # 等提示条自己消失，别挡住那张表
        await pg.evaluate(
            "document.querySelector('#historyTable').scrollIntoView({block:'center'})")
        await pg.wait_for_timeout(350)
        await pg.screenshot(path=OUT + "-m4-history-cleared.png")
        await pg.evaluate("window.scrollTo(0, 0)")
        await pg.wait_for_timeout(300)

        # 历史字典（表格折卡）
        await pg.click("#navToggle")   # 其余页在抽屉里，没有「更多」格了
        await pg.wait_for_timeout(300)
        await pg.click('.nav-item[data-tab="library"]')
        await pg.wait_for_timeout(800)
        await pg.screenshot(path=OUT + "-m5-library.png")

        # 设置 + 确认弹窗
        await pg.click("#navToggle")   # 其余页在抽屉里，没有「更多」格了
        await pg.wait_for_timeout(300)
        await pg.click('.nav-item[data-tab="settings"]')
        await pg.wait_for_timeout(500)

        # 代理卡：一个框里两行地址，点「测速并切换」拍真实的探测结果
        # （跑完清空，不留痕）
        await pg.fill("#pcAll", "http://192.168.1.10:7897\nhttp://192.168.1.11:7890")
        await pg.evaluate("document.getElementById('pcAll').blur()")
        await pg.wait_for_timeout(200)
        await pg.click("#btnPcTest")
        await pg.wait_for_timeout(9000)
        await pg.evaluate(
            "document.getElementById('pcAll').closest('.card')"
            ".scrollIntoView({block:'center'})")
        await pg.wait_for_timeout(400)
        await pg.screenshot(path=OUT + "-m6-proxy.png")
        await pg.fill("#pcAll", "")
        await pg.wait_for_timeout(200)
        # 改完要落盘，否则切页会被「有未保存的修改」弹窗拦下
        await pg.click("#btnCfgSave")
        await pg.wait_for_timeout(900)

        await pg.click("#btnBotRestart")
        await pg.wait_for_timeout(400)
        await pg.screenshot(path=OUT + "-m6-confirm.png")
        await pg.click("#modalCancel")
        await pg.wait_for_timeout(200)

        # 日志
        await pg.click("#navToggle")   # 其余页在抽屉里，没有「更多」格了
        await pg.wait_for_timeout(300)
        await pg.click('.nav-item[data-tab="logs"]')
        await pg.wait_for_timeout(600)
        await pg.screenshot(path=OUT + "-m7-logs.png")

        # 搜索（大输入框 + 旁边的搜索键）
        await pg.click('.tabbar-item[data-tab="search"]')
        await pg.wait_for_timeout(500)
        await pg.screenshot(path=OUT + "-m8-search.png")
        await pg.click("#searchAdv summary")
        await pg.wait_for_timeout(300)
        await pg.screenshot(path=OUT + "-m9-search-adv.png")

        # ---- 桌面 ----
        desk = await browser.new_context(viewport={"width": 1440, "height": 900})
        d = await desk.new_page()
        await d.goto(BASE, wait_until="networkidle")
        await d.wait_for_timeout(600)
        await d.screenshot(path=OUT + "-d1-overview.png")
        await d.click('.nav-item[data-tab="search"]')
        await d.wait_for_timeout(400)
        await d.screenshot(path=OUT + "-d2-search.png")
        await d.click('.nav-item[data-tab="settings"]')
        await d.wait_for_timeout(400)
        await d.evaluate(
            "document.getElementById('pcList').scrollIntoView({block:'center'})")
        await d.wait_for_timeout(300)
        await d.screenshot(path=OUT + "-d3-settings.png")

        # 控制台报错也算结果
        errors = []
        pg2 = await phone.new_page()
        pg2.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg2.on("pageerror", lambda e: errors.append(str(e)))
        await pg2.goto(BASE, wait_until="networkidle")
        await pg2.wait_for_timeout(800)
        print("JS 错误：", errors or "无")
        await browser.close()


asyncio.run(main())
