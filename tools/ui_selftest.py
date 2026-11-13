"""面板交互的自检：快捷开关是否真落盘、未保存保护是否会拦、确认弹窗是否会放行一次。"""
import asyncio
import json
import os
import urllib.request

from playwright.async_api import async_playwright

BASE = os.environ.get("TMB_UI_BASE", "http://127.0.0.1:8470/")
ok_count = 0
fail = []


def check(name, cond, extra=""):
    global ok_count
    if cond:
        ok_count += 1
        print("  ✓", name)
    else:
        fail.append(name + (" " + extra if extra else ""))
        print("  ✗", name, extra)


def get_cfg():
    with urllib.request.urlopen(BASE + "api/config") as r:
        return json.loads(r.read().decode())["config"]


def post_cfg(patch):
    """直接改配置（测试里用来把上一轮留下的脏数据清掉）。"""
    req = urllib.request.Request(
        BASE + "api/config", data=json.dumps({"config": patch}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


async def open_drawer(pg):
    """抽屉打开时会盖住汉堡按钮，所以先看状态再点，避免点到一半状态变了。"""
    opened = await pg.evaluate("document.getElementById('app').classList.contains('nav-open')")
    if not opened:
        await pg.click("#navToggle")
    await pg.wait_for_timeout(380)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto(BASE, wait_until="networkidle")
        await pg.wait_for_timeout(600)

        print("快捷开关")
        before = get_cfg()["download"].get("auto_download")
        await pg.click("#qAuto")
        await pg.wait_for_timeout(700)
        after = get_cfg()["download"].get("auto_download")
        check("点开关后配置真的变了", before != after, "%s -> %s" % (before, after))
        check("开关状态跟着回填", await pg.is_checked("#qAuto") == bool(after))
        await pg.click("#qAuto")
        await pg.wait_for_timeout(700)
        check("再点一次恢复原值", get_cfg()["download"].get("auto_download") == before)

        print("未保存保护")
        await open_drawer(pg)
        await pg.click('.nav-item[data-tab="settings"]')
        await pg.wait_for_timeout(500)
        await pg.fill("#tPoll", "45")
        await pg.wait_for_timeout(150)
        await open_drawer(pg)
        await pg.click('.nav-item[data-tab="library"]')
        await pg.wait_for_timeout(350)
        check("点菜单项后抽屉自动收起",
              not await pg.evaluate("document.getElementById('app').classList.contains('nav-open')"))
        check("切页时弹出「有未保存的修改」", await pg.is_visible("#modal"))
        check("弹窗标题正确", (await pg.inner_text("#modalTitle")) == "有未保存的修改")
        await pg.click("#modalCancel")
        await pg.wait_for_timeout(250)
        check("点取消留在原页", await pg.is_visible("#tab-settings"))
        # 再走一次，这次放弃修改
        await open_drawer(pg)
        await pg.click('.nav-item[data-tab="library"]')
        await pg.wait_for_timeout(300)
        await pg.click("#modalOk")
        await pg.wait_for_timeout(400)
        check("点放弃后切到目标页", await pg.is_visible("#tab-library"))


        print("未保存提示条")
        await open_drawer(pg)
        await pg.click('.nav-item[data-tab="settings"]')
        await pg.wait_for_timeout(450)
        await pg.fill("#tPoll", "46")
        await pg.wait_for_timeout(200)
        check("一改动就浮出提示条", await pg.is_visible("#saveBar"))
        check("提示条写明是哪一页", "设置" in (await pg.inner_text("#saveBarText")))
        await pg.click("#saveBarSave")
        await pg.wait_for_timeout(900)
        check("点保存真的落盘", get_cfg()["telegram"].get("poll_timeout") == 46)
        check("保存后提示条收起", not await pg.is_visible("#saveBar"))

        print("确认弹窗")
        # 「下载与迅雷」页已经撤掉入口，改用它旁边那个同样带 data-confirm 的按钮来验拦截机制
        await pg.click("#btnBotRestart")
        await pg.wait_for_timeout(300)
        check("危险按钮先弹确认", await pg.is_visible("#modal"))
        check("确认文案来自 HTML", "重启 Telegram 轮询" in (await pg.inner_text("#modalText")))
        await pg.click("#modalCancel")
        await pg.wait_for_timeout(250)
        check("取消后弹窗关闭", not await pg.is_visible("#modal"))
        check("取消后没有偷偷执行（按钮没被禁用）", not await pg.is_disabled("#btnBotRestart"))

        print("表格折卡")
        labels = await pg.evaluate("""() => {
          const t = document.getElementById('historyTable');
          const cells = Array.from(t.querySelectorAll('tbody td'));
          const real = cells.find(td => !td.classList.contains('empty'));
          if (!real) {
            return { empty: true, cols: t.querySelectorAll('thead th').length,
                     span: cells[0] ? cells[0].getAttribute('colspan') : null };
          }
          const names = cells.map(td => td.getAttribute('data-label'));
          return { empty: false, hasTitle: names.indexOf('标题') >= 0, labels: names.join('/') };
        }""")
        if labels["empty"]:
            # 干净的开发库里一条投递都没有：退一步验「空表那一行占满整行」，至少别假装验过了
            print("  · 暂无投递记录，改用空表兜底检查")
            check("空表占满整行", labels["span"] == str(labels["cols"]), str(labels))
        else:
            check("表头自动补成 data-label", labels.get("hasTitle"), str(labels))

        print("搜索页")
        await open_drawer(pg)
        await pg.click('.nav-item[data-tab="search"]')
        await pg.wait_for_timeout(450)
        check("更多选项默认收起", not await pg.evaluate("document.getElementById('searchAdv').open"))
        check("收起时摘要写明当前条件", "做种" in (await pg.inner_text("#sAdvSummary")))
        await pg.evaluate("const el=document.getElementById('sLimit');el.value=20;"
                          "el.dispatchEvent(new Event('change'))")
        await pg.wait_for_timeout(200)
        check("改条数后摘要跟着变", "20 条" in (await pg.inner_text("#sAdvSummary")))
        await pg.evaluate("const el=document.getElementById('sLimit');el.value=10;"
                          "el.dispatchEvent(new Event('change'))")
        check("搜索键就在输入框右边同一行", await pg.evaluate(
            """() => {
                 const t = document.getElementById('sQuery').getBoundingClientRect();
                 const b = document.getElementById('btnSearch').getBoundingClientRect();
                 return b.left >= t.right - 1 && Math.abs(b.top - t.top) < 4;
               }"""))
        check("「解析后搜」是紧挨着的第二个动作", await pg.evaluate(
            """() => {
                 const a = document.getElementById('btnSearch').getBoundingClientRect();
                 const b = document.getElementById('btnParseSearch').getBoundingClientRect();
                 return b.top >= a.bottom - 1 && Math.abs(b.left - a.left) < 2;
               }"""))
        check("输入框够高（能贴一整段）", await pg.evaluate(
            "document.getElementById('sQuery').getBoundingClientRect().height") >= 80)
        # 无头环境没有剪贴板权限：按下去要么粘上，要么明确告诉用户怎么手动粘，不能装死
        await pg.click("#btnPaste")
        await pg.wait_for_timeout(400)
        check("点粘贴有回应（粘上或告诉你怎么粘）", await pg.is_visible("#toast"))
        await pg.fill("#sQuery", "test 1080p")
        await pg.wait_for_timeout(200)
        check("有字时出现「清空」", await pg.is_visible("#btnClearQuery"))
        await pg.click("#btnClearQuery")
        await pg.wait_for_timeout(200)
        check("清空后按钮收起", not await pg.is_visible("#btnClearQuery"))

        print("磁力解析")
        # 粘整条磁力时，搜索键自己变成「解析磁力」——磁力没有关键词可搜，
        # 当普通文本送去检索只会搜出一堆无关结果
        await pg.fill("#sQuery",
                      "magnet:?xt=urn:btih:E8BE27D6280895F4838832CF84D90451B69B3AFC")
        await pg.wait_for_timeout(300)
        check("粘磁力后搜索键变成「解析磁力」",
              (await pg.inner_text("#btnSearch")).strip() == "解析磁力",
              await pg.inner_text("#btnSearch"))
        check("磁力模式下收起「解析后搜」", not await pg.is_visible("#btnParseSearch"))
        check("提示改成「回车即解析」", "解析" in (await pg.inner_text("#sqTipText")))
        await pg.click("#btnClearQuery")
        await pg.wait_for_timeout(300)
        check("清空后恢复「直接搜索」",
              (await pg.inner_text("#btnSearch")).strip() == "直接搜索",
              await pg.inner_text("#btnSearch"))
        check("清空后「解析后搜」回来", await pg.is_visible("#btnParseSearch"))

        print("解析后搜")
        # 自带磁力的消息一步都不该往站点跑，正好拿来在无网环境里验解析结果
        await pg.fill("#sQuery",
                      "FC2PPV-4999948 某作品\n"
                      "magnet:?xt=urn:btih:E8BE27D6280895F4838832CF84D90451B69B3AFC")
        await pg.click("#btnParseSearch")
        await pg.wait_for_timeout(1500)
        check("解析结果当场摆出来", await pg.is_visible("#parseBox"))
        check("判定写明是「自带磁力」", "自带磁力" in (await pg.inner_text("#parseBox")))
        check("磁力条目旁边就能投递", await pg.is_visible("#parseBox [data-mag-dl]"))
        check("自带磁力不去站点搜（结果区是空的）",
              (await pg.inner_text("#searchResults")).strip() == "")
        await pg.click("#btnClearQuery")
        await pg.wait_for_timeout(250)
        check("清空后解析结果跟着收起", not await pg.is_visible("#parseBox"))
        h = await pg.evaluate("document.querySelector('.topbar').getBoundingClientRect().height")
        check("手机顶栏不超过 60px", h <= 60, "实测 %.0fpx" % h)
        # 内嵌 webview 会把状态栏+原生标题栏的高度也报成 safe-area-inset-top，
        # 顶栏要是直接吃这个值，标题下面就会多出一大条空白
        pad = await pg.evaluate(
            "getComputedStyle(document.querySelector('.topbar')).paddingTop")
        check("顶栏顶部内边距只有 8px", pad == "8px", str(pad))

        print("导航：底部两格 + 抽屉里全都在")
        await pg.click('.tabbar-item[data-tab="overview"]')
        await pg.wait_for_timeout(500)
        check("底部标签栏只剩 2 格", await pg.evaluate(
            "document.querySelectorAll('.tabbar-item').length") == 2)
        check("「更多」那一格已经去掉", await pg.evaluate(
            "!document.getElementById('tabbarMore')"))
        await open_drawer(pg)
        side = await pg.evaluate(
            "Array.from(document.querySelectorAll('.nav-item')).map(b => b.dataset.tab).join(',')")
        check("抽屉里七个页面都在", side ==
              "overview,rules,search,library,download,settings,logs", side)
        await pg.click('.nav-item[data-tab="rules"]')
        await pg.wait_for_timeout(700)
        check("从抽屉能进「关键词规则」", await pg.is_visible("#tab-rules"))
        await open_drawer(pg)
        await pg.click('.nav-item[data-tab="download"]')
        await pg.wait_for_timeout(700)
        check("从抽屉能进「下载与迅雷」", await pg.is_visible("#tab-download"))
        await pg.click('.tabbar-item[data-tab="overview"]')
        await pg.wait_for_timeout(600)

        print("概览：下载后端与连接状态")
        be = get_cfg()["download"].get("backend")
        backend_txt = (await pg.inner_text("#statBackend")).strip()
        check("卡片写着下载后端", backend_txt != "", backend_txt)
        if be == "xunlei":
            check("后端名显示「迅雷」", backend_txt == "迅雷", backend_txt)
        check("副标题是连接状态（不是「兜底 无」）",
              (await pg.inner_text("#statBackendSub")).strip() != "")
        check("快捷开关里有「立即重连」", await pg.is_visible("#qXlReconnect"))
        check("按钮旁边就能看到状态", (await pg.inner_text("#qXlState")).strip() != "")

        print("最近投递：清除按钮")
        check("表格旁边有「清除」", await pg.is_visible("#btnHistClear"))
        # 塞一行假记录，确认清除是按了就把屏幕清掉（不去问后端要删什么）
        await pg.evaluate(
            "document.querySelector('#historyTable tbody').innerHTML ="
            " '<tr><td>01-01 00:00</td><td>假记录</td><td>迅雷</td><td>t1</td><td>ok</td></tr>'")
        await pg.click("#btnHistClear")
        await pg.wait_for_timeout(300)
        body = await pg.evaluate("document.querySelector('#historyTable tbody').textContent")
        check("点清除后表格空了", "已清除" in body, body)
        check("说明里指明记录没被删", "只是清屏" in body, body)
        check("提示告诉你怎么再看到", "刷新" in (await pg.inner_text("#historyHint")))
        # 切走再回来不该又铺回来（否则看着像没生效）
        await pg.click('.tabbar-item[data-tab="search"]')
        await pg.wait_for_timeout(300)
        await pg.click('.tabbar-item[data-tab="overview"]')
        await pg.wait_for_timeout(800)
        check("切页回来仍是清的", "已清除" in await pg.evaluate(
            "document.querySelector('#historyTable tbody').textContent"))
        # 手动刷新要能重新载回来
        await pg.click("#btnReload")
        await pg.wait_for_timeout(900)
        check("点刷新能重新载入", "已清除" not in await pg.evaluate(
            "document.querySelector('#historyTable tbody').textContent"))

        print("最近投递：复制磁力")
        await ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        # 先点一次「清除」：histCleared 一置位，8 秒轮询就不会把下面这两行假记录冲掉
        await pg.click("#btnHistClear")
        await pg.wait_for_timeout(250)
        await pg.evaluate("""() => {
          const mk = (title, mg) =>
            '<tr><td class="pick"><input type="checkbox" class="hpick" data-magnet="' + mg + '"></td>'
            + '<td>11-13 12:00</td>'
            + '<td><span class="ellipsis">' + title + '</span></td>'
            + '<td>迅雷</td><td>t9</td><td><span class="tag ok">已投递</span></td>'
            + '<td class="copy-col"><button class="btn ghost sm hcopy" data-magnet="' + mg + '">复制</button></td></tr>';
          document.querySelector('#historyTable tbody').innerHTML =
            mk('甲种子', 'magnet:?xt=urn:btih:aaa') + mk('乙种子', 'magnet:?xt=urn:btih:bbb');
        }""")
        await pg.wait_for_timeout(200)
        check("每条记录都有「复制」",
              (await pg.eval_on_selector_all("#historyTable .hcopy", "els => els.length")) == 2)
        # 假行是直接塞进 DOM 的，勾一下第一条让按钮状态自己算一遍（真实流程是渲染完就同步）
        await pg.evaluate("""() => {
          const b = document.querySelector('#historyTable .hpick');
          b.checked = true;
          b.dispatchEvent(new Event('change', { bubbles: true }));
        }""")
        await pg.wait_for_timeout(200)
        check("勾一条后按钮变成「复制 1 条」",
              "1 条" in (await pg.inner_text("#btnHistCopySel")))

        await pg.click("#historyTable tbody tr:nth-child(1) .hcopy")
        await pg.wait_for_timeout(350)
        clip = await pg.evaluate("navigator.clipboard.readText()")
        check("单条复制拿到这条磁力", clip.strip() == "magnet:?xt=urn:btih:aaa", clip)
        check("复制后给了提示", await pg.is_visible("#toast"))

        await pg.click("#btnHistAll")
        await pg.wait_for_timeout(250)
        check("全选勾上两条",
              (await pg.eval_on_selector_all("#historyTable .hpick:checked", "e => e.length")) == 2)
        check("按钮写明选了几条", "2 条" in (await pg.inner_text("#btnHistCopySel")))
        check("全选后按钮改成「取消全选」", "取消" in (await pg.inner_text("#btnHistAll")))
        await pg.click("#btnHistCopySel")
        await pg.wait_for_timeout(350)
        clip = await pg.evaluate("navigator.clipboard.readText()")
        # Windows 剪贴板会把 \n 存成 \r\n，粘到迅雷里是一样的，别为这个判死
        check("多选复制一行一条",
              clip.replace("\r\n", "\n") == "magnet:?xt=urn:btih:aaa\nmagnet:?xt=urn:btih:bbb", repr(clip))
        await pg.click("#btnHistAll")
        await pg.wait_for_timeout(250)
        check("再点一次全不选",
              (await pg.eval_on_selector_all("#historyTable .hpick:checked", "e => e.length")) == 0)
        check("没勾选时按钮禁用", await pg.is_disabled("#btnHistCopySel"))

        row_h = await pg.evaluate(
            "document.querySelector('#historyTable tbody tr').getBoundingClientRect().height")
        check("一条记录压在两行里（不再一列一行）", row_h <= 66, "height=%.0fpx" % row_h)

        print("顶栏不吸顶")
        await pg.evaluate("window.scrollTo(0, 700)")
        await pg.wait_for_timeout(350)
        top = await pg.evaluate("document.querySelector('.topbar').getBoundingClientRect().top")
        check("滚动后顶栏跟着上移", top < -20, "top=%.0fpx" % top)
        await pg.evaluate("window.scrollTo(0, 0)")
        await pg.wait_for_timeout(250)

        print("代理：一个框，一行一个地址，不分主备")
        await open_drawer(pg)
        await pg.click('.nav-item[data-tab="settings"]')
        await pg.wait_for_timeout(500)
        # 上一轮跑崩了会留下地址、或者把「自动切换」关掉过：先归零，
        # 否则「空着就是直连」这些断言验的就不是新装的默认状态
        cur = get_cfg()
        if ((cur.get("proxy_channels") or []) or cur.get("proxy_auto") is False
                or (cur.get("telegram") or {}).get("proxy")
                or (cur.get("nyaa") or {}).get("proxy")):
            post_cfg({"proxy_channels": [], "proxy_auto": True,
                      "telegram": {"proxy": ""}, "nyaa": {"proxy": ""}})
            await pg.reload(wait_until="load")
            await pg.wait_for_timeout(900)
            await open_drawer(pg)
            await pg.click('.nav-item[data-tab="settings"]')
            await pg.wait_for_timeout(500)
        check("设置页有代理输入框", await pg.is_visible("#pcAll"))
        check("只有一个框，不是每个代理一行输入", await pg.evaluate(
            "document.querySelectorAll('#tab-settings textarea').length === 1"))
        check("老的「主代理 / 检索代理」输入框都没了", await pg.evaluate(
            "!document.getElementById('tProxy') && !document.getElementById('nProxy')"))
        card_txt = await pg.evaluate(
            "document.getElementById('pcAll').closest('.card').innerText")
        check("卡片里写明从上往下试", "从上往下试" in card_txt, card_txt[:80])
        check("卡片里写明互为备胎", "互为备胎" in card_txt, card_txt[:80])
        check("卡片里写明内网地址永远直连", "内网" in card_txt)
        check("卡片里不再分主备", "主代理" not in card_txt and "备用通道" not in card_txt)
        check("按钮写的是「测速并切换」",
              (await pg.inner_text("#btnPcTest")).strip() == "测速并切换")
        check("自动切换默认开着", await pg.is_checked("#pcAuto"))
        check("没填地址时列表说清「就是直连」", await pg.evaluate(
            "!!document.querySelector('#pcList .empty')"))

        # 填两条都连不上的地址：正好验证「每条都会说话」「都死了才落到直连」
        await pg.fill("#pcAll", "127.0.0.1:7899\n127.0.0.1:7898")
        # 结果列表在失焦（change）后才重画：边打字边刷会把「填了一半的地址」当成一条线
        await pg.evaluate("document.getElementById('pcAll').blur()")
        await pg.wait_for_timeout(250)
        check("改了线路会提示未保存", await pg.is_visible("#saveBar"))
        check("提示里写明有几条", "2 条" in (await pg.inner_text("#pcCount")),
              await pg.inner_text("#pcCount"))
        check("两条各占一行", await pg.evaluate(
            "document.querySelectorAll('#pcList .line-row').length") == 2)
        # 少写端口这种最常见的笔误，别让它安安静静地失效
        await pg.fill("#pcAll", "127.0.0.1:7899\n192.168.1.11")
        await pg.evaluate("document.getElementById('pcAll').blur()")
        await pg.wait_for_timeout(250)
        bad_row = (await pg.inner_text("#pcList .line-row:nth-child(2)")).strip()
        check("没写端口的那行当场报格式不对", "格式不对" in bad_row, bad_row)
        check("提示里数出坏行", "格式不对" in (await pg.inner_text("#pcCount")),
              await pg.inner_text("#pcCount"))
        await pg.fill("#pcAll", "127.0.0.1:7899\n127.0.0.1:7898")
        await pg.evaluate("document.getElementById('pcAll').blur()")
        await pg.wait_for_timeout(250)

        # 点一下：先落盘，再把每条 + 直连一起测，最后挑一条能用的
        await pg.click("#btnPcTest")
        await pg.wait_for_timeout(12000)      # 直连测不通要等一个超时窗口
        rows = await pg.eval_on_selector_all(
            "#pcList .line-row .ch-res", "e => e.map(x => x.textContent.trim())")
        check("每条都写了测速结果", len(rows) == 2 and all(rows) and "…" not in rows, str(rows))
        check("连不上的地址被标成不通", all(r.startswith("✗") for r in rows), str(rows))
        st_txt = (await pg.inner_text("#pcState")).strip()
        check("状态行说清这次走谁（或已直连）", st_txt not in ("", "—"), st_txt)
        check("都死了就落到直连", "直连" in st_txt, st_txt)
        cfg_now = get_cfg()
        check("测速前先把改动落盘了（第一行进 telegram.proxy）",
              "7899" in (cfg_now["telegram"].get("proxy") or ""),
              str(cfg_now["telegram"].get("proxy")))
        chs = cfg_now.get("proxy_channels") or []
        check("第二行进通道表", len(chs) == 1 and "7898" in chs[0]["url"], str(chs))
        check("检索代理并进线路表后不再单独存",
              (cfg_now.get("nyaa", {}).get("proxy") or "") == "",
              str(cfg_now.get("nyaa", {}).get("proxy")))

        # 自动切换开关落盘（关掉再打开，确认能写进去也能读出来）。
        # 用真的点：合成事件不冒泡，页面不会记成「未保存」，保存条也就不会出来
        await pg.uncheck("#pcAuto")
        await pg.wait_for_timeout(250)
        check("改开关会提示未保存", await pg.is_visible("#saveBar"))
        # 这里只关心「保存后配置真的变了」，用 JS 直接触发按钮（同一个 click 处理函数）；
        # 保存条的点击命中在别的段落已经验过了
        await pg.evaluate("document.getElementById('saveBarSave').click(); 1")
        await pg.wait_for_timeout(900)
        check("关掉自动切换会写进配置", get_cfg().get("proxy_auto") is False,
              str(get_cfg().get("proxy_auto")))
        await pg.check("#pcAuto")
        await pg.wait_for_timeout(250)
        await pg.evaluate("document.getElementById('saveBarSave').click(); 1")
        await pg.wait_for_timeout(900)
        check("再打开也存得住", get_cfg().get("proxy_auto") is True,
              str(get_cfg().get("proxy_auto")))

        # 清空 = 直连，配置里一条都不该剩（别把测试用的死地址留在机器上）
        await pg.fill("#pcAll", "")
        await pg.wait_for_timeout(250)
        check("清空后提示变回「就是直连」",
              "直连" in (await pg.inner_text("#pcCount")), await pg.inner_text("#pcCount"))
        await pg.click("#saveBarSave")
        await pg.wait_for_timeout(800)
        check("地址真的从配置里没了",
              (get_cfg().get("proxy_channels") or []) == []
              and (get_cfg()["telegram"].get("proxy") or "") == "",
              str(get_cfg().get("proxy_channels")) + " / " + str(get_cfg()["telegram"].get("proxy")))

        print("文件筛选（从抽屉进下载页，逻辑仍作用于自动投递）")
        await open_drawer(pg)
        await pg.click('.nav-item[data-tab="download"]')
        await pg.wait_for_timeout(600)
        await pg.evaluate("document.getElementById('xFOn').checked=false;"
                          "document.getElementById('xFOn').dispatchEvent(new Event('change'))")
        await pg.wait_for_timeout(200)
        check("关掉筛选后提示一律全下", "一律全下" in
              (await pg.evaluate("document.getElementById('xFilterState').textContent")))
        check("关掉筛选后整块变淡", await pg.evaluate(
            "document.getElementById('xFilterBox').classList.contains('off')"))
        await pg.evaluate("document.getElementById('xFOn').checked=true;"
                          "document.getElementById('xFOn').dispatchEvent(new Event('change'))")
        await pg.wait_for_timeout(200)
        check("打开后改成按规则挑", "按下面规则挑" in
              (await pg.evaluate("document.getElementById('xFilterState').textContent")))
        check("开关长在筛选区里（不是另立一个「全选」）", await pg.evaluate(
            "!!document.getElementById('xFOn').closest('#xFilterBox')"))
        check("设置页不再有磁力解析输入框", not await pg.is_visible("#ffTestMagnet"))
        # 刚才那两下算「改动」，不清掉的话下一步切页会被「未保存」弹窗拦住
        await pg.click("#saveBarDiscard")
        await pg.wait_for_timeout(600)

        print("抽屉")
        await open_drawer(pg)
        check("抽屉打开", await pg.evaluate("document.getElementById('app').classList.contains('nav-open')"))
        await pg.mouse.click(370, 400)
        await pg.wait_for_timeout(350)
        check("点空白处收起", not await pg.evaluate("document.getElementById('app').classList.contains('nav-open')"))
        check("收起后恢复页面滚动", await pg.evaluate("document.body.style.overflow === ''"))

        check("没有 JS 报错", not errs, str(errs))
        await b.close()

    print("\n通过 %d 项，失败 %d 项" % (ok_count, len(fail)))
    if fail:
        for f in fail:
            print("  -", f)
        raise SystemExit(1)


asyncio.run(main())
