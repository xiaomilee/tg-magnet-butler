"""面板前端的两条底线：元素 id 不能对不上，手机端该有的东西不能丢。

改 HTML 结构最容易犯的错是「删了/改了 id，JS 里还在找它」——桌面端可能看不出来，
到手机上才发现某个按钮点了没反应。这里做静态核对，不跑浏览器。
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "app", "ui")


def read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as f:
        return f.read()


class TestMarkup(unittest.TestCase):
    def setUp(self):
        self.html = read("index.html")
        self.js = read("app.js")
        self.css = read("style.css")

    def test_partial_line_is_amber_not_red(self):
        """「通，只是某个站到不了」要画成琥珀色，不能画成红色。

        红色（warn-text）在这个面板里代表「这条线路废了」。线路明明能用、只是
        检索站被 Clash 拒了，画成红色会让人去重配代理，白折腾。
        """
        self.assertIn("partial-text", self.js)
        self.assertIn(".ch-res.partial-text", self.css)
        self.assertIn("var(--warn)", self.css.split(".ch-res.partial-text")[1][:200])

    def test_js_ids_exist(self):
        """app.js 里 $("x") / setVal("x") 找到的 id，HTML 里必须真有。"""
        ids = set(re.findall(r'id="([^"]+)"', self.html))
        used = set()
        for fn in ("$", "val", "num", "checked", "setVal", "setChecked"):
            used |= set(re.findall(fn.replace("$", r"\$") + r'\("([A-Za-z0-9_\-]+)"\)', self.js))
        self.assertTrue(used, "没能从 app.js 里解析出任何 id 引用，检查正则")
        missing = sorted(u for u in used if u not in ids)
        self.assertEqual(missing, [], "这些 id 在 HTML 里不存在：%s" % missing)

    def test_tabbar_targets_exist(self):
        """底部标签栏的每个 data-tab 都要有对应的区块，否则点了是空白页。"""
        sections = set(re.findall(r'<section id="tab-([a-z]+)"', self.html))
        for name in re.findall(r'<button class="tabbar-item[^"]*" data-tab="([a-z]+)"', self.html):
            self.assertIn(name, sections)
        self.assertEqual(len(re.findall(r'class="tabbar-item', self.html)), 2,
                         "底部导航只留概览 + 搜索两格")

    def test_drawer_lists_every_page(self):
        """底部只有两格，所以「规则」「下载」这些页必须都能从抽屉点到——少一个就等于丢了。"""
        bar = self.html.split('id="tabbar"')[1].split("</nav>")[0]
        self.assertEqual(re.findall(r'data-tab="([a-z]+)"', bar), ["overview", "search"])
        self.assertNotIn('id="tabbarMore"', self.html, "「更多」这一格已经去掉了")
        self.assertNotIn("tabbarMore", self.js)
        # 抽屉（侧栏）里七个页面一个不少，顺序也照旧
        side = self.html.split('class="sidebar"')[1].split("</nav>")[0]
        self.assertEqual(re.findall(r'data-tab="([a-z]+)"', side),
                         ["overview", "rules", "search", "library", "download",
                          "settings", "logs"])
        self.assertNotIn("data-retired", self.html, "规则/下载页已恢复入口，不该再打 retired 标记")

    def test_history_clear_button(self):
        """「最近投递」要能一键清屏：只清展示，别删数据库里的记录。"""
        self.assertIn('id="btnHistClear"', self.html)
        self.assertIn('"btnHistClear"', self.js)
        self.assertIn("function clearHistory", self.js)
        # 清屏用「已清除」这种说法，别让人以为记录被删了
        self.assertIn("数据库里的记录一条都不删", self.html)
        self.assertIn("只是清屏", self.js)

    def test_history_copy_magnet(self):
        """「最近投递」要能把磁力抄出来：单条复制 + 勾选多条一次复制（一行一条）。

        用途很具体：迅雷额度用完之后，用户要靠这些链接去套件里手动加，
        所以本页必须留着原始磁力，不能只显示标题。
        """
        for i in ("btnHistAll", "btnHistCopySel", "historyTable"):
            self.assertIn('id="%s"' % i, self.html)
        head = self.html.split('id="historyTable"')[1].split("</thead>")[0]
        self.assertIn('class="pick"', head)
        self.assertIn('class="copy-col"', head)
        self.assertEqual(len(re.findall(r"<th[ >]", head)), 7, "勾选 + 5 个字段 + 磁力 = 7 列")
        self.assertIn('colspan="7"', self.html)
        # 有磁力就用磁力；只留了 infohash 的老记录也能拼一条出来
        self.assertIn("function histMagnet", self.js)
        self.assertIn("magnet:?xt=urn:btih:", self.js)
        # 面板常从内网 http 打开，那里没有 clipboard API，必须留 execCommand 兜底
        self.assertIn('execCommand("copy")', self.js)
        self.assertIn("isSecureContext", self.js)
        # 多条之间用换行分隔，迅雷那边才能逐条认
        self.assertIn('arr.join("\\n")', self.js)
        # 手机上压成两行（一列一行会滑到手指疼），任务号那列直接藏掉
        mobile = self.css.split("@media (max-width:760px)")[1]
        self.assertIn("grid-template-areas", mobile)
        self.assertIn("#historyTable td:nth-child(5){display:none}", mobile)

    def test_viewport_covers_notch(self):
        self.assertIn("viewport-fit=cover", self.html)
        self.assertIn("theme-color", self.html)

    def test_mobile_media_query(self):
        self.assertIn("@media (max-width:760px)", self.css)
        # 抽屉、底部栏、弹窗三件套的样式都得在
        for sel in (".tabbar", ".nav-shade", ".modal-card", ".table td[data-label]"):
            self.assertIn(sel, self.css, "缺少手机端样式：%s" % sel)

    def test_inputs_big_enough_for_ios(self):
        """iOS 上输入框小于 16px 会在聚焦时把整页放大，这条别被改回去。"""
        block = self.css.split("@media (max-width:760px)")[1]
        self.assertIn("font-size:16px", block.split("}")[0] + block)
        self.assertIn("input[type=text]", block)

    def test_touch_targets(self):
        block = self.css.split("@media (max-width:760px)")[1]
        self.assertIn("min-height:42px", block)      # 按钮
        self.assertIn("min-height:48px", block)      # 开关行

    def test_confirm_wired(self):
        """危险动作要有 data-confirm 文案，并且脚本里真的拦了它。"""
        self.assertGreaterEqual(len(re.findall(r"data-confirm=", self.html)), 3)
        self.assertIn('closest("[data-confirm]")', self.js)
        self.assertIn("function askConfirm", self.js)

    def test_unsaved_guard(self):
        self.assertIn("state.dirty", self.js)
        self.assertIn("beforeunload", self.js)
        self.assertIn("有未保存的修改", self.js)

    def test_quick_switches(self):
        """概览页的快捷开关：三个开关 + 暂停按钮 + 迅雷重连，别在改版时丢掉。"""
        for i in ("qAuto", "qLibCheck", "qLibSkip", "qBotPause", "qXlReconnect", "qXlState"):
            self.assertIn('id="%s"' % i, self.html)
            self.assertIn('"%s"' % i, self.js)
        # 下载页撤掉后，「立即重连」就靠这一格：按钮和状态字必须在一起
        quick = self.html.split('class="card quick"')[1].split('id="historyHint"')[0]
        self.assertIn('id="qXlReconnect"', quick)
        self.assertIn('id="qXlState"', quick)

    def test_search_page_hero(self):
        """搜索页：关键词是大文本框，搜索键就在旁边，其它条件收进可折叠的「更多选项」。"""
        self.assertRegex(self.html, r'<textarea id="sQuery"[^>]*rows="3"')
        for i in ("btnPaste", "btnSearch", "btnParseSearch", "searchAdv", "sAdvSummary",
                  "parseBox"):
            self.assertIn('id="%s"' % i, self.html)
        # 四个条件 + 两个诊断按钮必须都落在 details 里面，否则「收起就看不到」这条不成立
        adv = self.html.split('id="searchAdv"')[1].split("</details>")[0]
        for i in ("sCategory", "sSort", "sSeeders", "sLimit", "btnDiag", "btnNyaaProbe"):
            self.assertIn('id="%s"' % i, adv, "「更多选项」里少了 %s" % i)
        # 收起时也要能读到当前条件，不然「怎么只搜到 5 条」没处查
        self.assertIn("function syncSearchAdv", self.js)

    def test_search_split_direct_and_parsed(self):
        """搜索拆成两条路：直接搜关键词 / 先解析再搜。解析那条必须把判定结果摆出来，
        而且用规则里的条件（不是面板里那几个框），不然试出来的结果跟机器人跑的对不上。"""
        self.assertIn("直接搜索", self.html)
        self.assertIn("解析后搜", self.html)
        self.assertIn("function doParseSearch", self.js)
        # 解析结果面板：判定徽章、查询串、命中规则、磁力条目都要能渲染
        for marker in ("pb-badge", "pb-row", "pb-mag", "pb-note", "renderParse("):
            self.assertIn(marker, self.js, "解析结果里少了 %s" % marker)
        self.assertIn(".parse-box", self.css)
        self.assertIn(".pb-mag", self.css)
        # 自带磁力的消息不该再去站点跑一趟：后端直接给 note，前端据此不渲染结果
        src = read(os.path.join("..", "tmb", "webui.py"))
        self.assertIn('"/api/search/parsed"', src)
        self.assertIn("检索用的是上面这条查询", self.js)

    def test_paste_degrades_gracefully(self):
        """一键粘贴在内网 http 下读不到剪贴板，必须退化成「教人手动粘」而不是静默失败。"""
        self.assertIn("navigator.clipboard", self.js)
        self.assertIn("function manualPaste", self.js)
        self.assertIn("长按输入框", self.js)
        # 多行输入框里回车是搜索，换行得用 Shift+回车
        self.assertIn("!e.shiftKey", self.js)

    def test_topbar_compact(self):
        """顶栏压成一行：说明文字只在概览页占位，手机上别的页面不该再多占一行高度。"""
        self.assertIn('id="topbar"', self.html)
        block = self.css.split("@media (max-width:760px)")[1]
        self.assertIn(".topbar:not(.show-desc) #pageDesc{display:none}", block)
        self.assertIn('show-desc', self.js)
        # 顶栏跟着页面一起滚上去（不吸顶）：手机上翻内容时能多看到一行
        for chunk in (self.css.split(".topbar{")[1].split("}")[0],
                      block.split(".topbar{")[1].split("}")[0]):
            self.assertNotIn("position:sticky", chunk, "顶栏不该再吸顶")
            self.assertNotIn("position:fixed", chunk, "顶栏不该改成浮层")
        self.assertNotIn("stuck", self.css)

    def test_no_double_top_safe_area(self):
        """飞牛 App 的内嵌 webview 会把「状态栏 + 原生标题栏」整段高度报成
        safe-area-inset-top，而页面本来就排在标题栏下方 —— 再补一次就是标题下面
        一大条空白（实测 96px）。所以顶栏/抽屉只认 --sat，且 --sat 只在独立 PWA 下取真值。
        """
        self.assertIn("--sat:0px", self.css)
        self.assertIn("@media (display-mode:standalone)", self.css)
        self.assertIn("--sat:env(safe-area-inset-top,0px)", self.css.replace(" ", ""))
        block = self.css.split("@media (max-width:760px)")[1]
        for sel in (".topbar{", ".sidebar{"):
            chunk = block.split(sel)[1].split("}")[0]
            self.assertNotIn("env(safe-area-inset-top)", chunk,
                             "%s 直接吃了 env(顶部安全区)，内嵌 webview 会重复留白" % sel)

    def test_filter_switch_lives_in_filter_box(self):
        """文件筛选只有一个总开关，而且就在筛选区里 —— 以前它叫「多文件种子全选」，
        挂在筛选区外面，很容易让人以为筛选只管某一条投递路径。"""
        self.assertIn('id="xFOn"', self.html)
        self.assertNotIn('id="xSelAll"', self.html, "「全选」这个反义开关已经并进筛选区了")
        block = self.html.split('id="xFilterBox"')[1].split("</label>")[0]
        self.assertIn('id="xFOn"', block, "总开关必须在筛选区内部")
        # 面板上要写清作用范围：三条投递路径都走同一份规则
        for path in ("自动投递", "补投", "直接发磁力链接"):
            self.assertIn(path, self.html, "筛选说明里没提「%s」这条路径" % path)
        # 存储字段仍是 select_all_files（反义），别在别处又造一个同义字段
        self.assertIn('select_all_files: !checked("xFOn")', self.js)

    def test_magnet_paste_turns_search_into_inspect(self):
        """磁力解析入口在**搜索页**，不在设置页。

        原来的「解析测试」压在文件筛选卡片里，粘一段磁力就顶出「设置未保存」，
        还得先想到去那儿找。现在并进搜索框：粘整条磁力，搜索键变成「解析磁力」。
        """
        # 设置页不该再有那块输入框（它的 input 事件会把设置标成「未保存」）
        self.assertNotIn('id="ffTestMagnet"', self.html)
        self.assertNotIn('id="ffTest"', self.html)

        # 搜索页要有切换用的提示位与判定函数
        self.assertIn('id="sqTipText"', self.html)
        self.assertIn("function isMagnet(", self.js)
        self.assertIn('"解析磁力"', self.js)
        # 两个入口都要先看是不是磁力，别把磁力当关键词送去检索
        self.assertIn("if (isMagnet(q)) { doMagnetInspect(q); return; }", self.js)
        self.assertEqual(self.js.count("doMagnetInspect(q)"), 2)

        self.assertIn('"/xunlei/parse"', self.js)
        # 三种认不出来的情形都要能说清，尤其「只认出 1 个文件」这种最常见的假筛选
        for text in ("一个文件都没认出来", "只认出 1 个文件", "按当前规则挑"):
            self.assertIn(text, self.js)

    def test_xunlei_has_no_panel_port_options(self):
        """1.0.31：设置页不再让人选「2345 面板」或填端口——只接飞牛官方套件。

        这些输入框每多一个，用户就多一次选错的机会（选了就是 errno 111）。
        端口由启动时的自动侦测决定，面板只显示结果。
        """
        for gone in ('id="xMode"', 'id="xPort"', 'id="xFnPort"', 'id="xHost"'):
            self.assertNotIn(gone, self.html, "设置页不该再有 %s" % gone)
        self.assertIn('id="btnXlReconnect"', self.html)
        self.assertIn('id="xConnState"', self.html)
        # 保存时必须固定网关模式，不能被旧配置带着又写回 panel
        self.assertIn('mode: "fnos_gateway"', self.js)
        self.assertIn('/xunlei/reconnect', self.js)

    def test_proxy_card_holds_everything(self):
        """代理只有一处：一个框、一行一个地址，不分主备。

        以前主代理在「Telegram」卡、检索代理在「站点与去重」卡、备用通道单开一张，
        同一台 Clash 要填两遍，还看不出「先试谁」。
        """
        for i in ("pcAll", "pcList", "btnPcTest", "pcState", "pcAuto",
                  "pcCount", "pcTestOut"):
            self.assertIn('id="%s"' % i, self.html)
        # 老的三处入口都该消失了：分开填就是这次要干掉的
        for gone in ('id="tProxy"', 'id="nProxy"', 'id="pcPrimaryRes"', 'id="btnPcAdd"'):
            self.assertNotIn(gone, self.html, "代理卡不该再有 %s" % gone)
        self.assertNotIn("主代理", self.html)
        self.assertNotIn("备用通道", self.html)
        # 卡片内顺序 = 从填到测：输入框 → 结果列表 → 自动切换
        card = self.html.split('<h3>代理</h3>')[1].split("<!-- ============")[0]
        self.assertLess(card.index('id="pcAll"'), card.index('id="pcList"'))
        self.assertLess(card.index('id="pcList"'), card.index('id="pcAuto"'))
        self.assertIn("<textarea", card)
        # 面板上直接给出最常见的写法，省一轮问答
        self.assertIn("http://192.168.1.11:7890", card)
        # 「测速并切换」是一个动作，不是两个按钮
        self.assertIn("测速并切换", card)
        self.assertNotIn(">测速<", card)

    def test_proxy_js_wiring(self):
        """前端：读输入框 → 落盘 → 调 /proxy/test → 把每条的结果画回去。"""
        self.assertIn("function readProxyLines", self.js)
        self.assertIn("function paintLineResults", self.js)
        self.assertIn("function testProxyLines", self.js)
        # 一个框的几行落到配置里：第一行 telegram.proxy，后面几行 proxy_channels
        self.assertIn("plines[0]", self.js)
        self.assertIn("proxy_channels: chans", self.js)
        self.assertIn("proxy_auto: checked(\"pcAuto\")", self.js)
        # 检索代理并进线路表，前端不再单独写它
        self.assertIn('proxy: ""', self.js)
        # 空行不算一条线路
        self.assertIn("filter(function (s) { return s.length > 0; })", self.js)
        # 整串测速 + 切换：所有线路和直连一次测完
        self.assertIn("/proxy/test", self.js)
        self.assertIn("lines: readProxyLines()", self.js)
        self.assertIn("preferred_kind", self.js)
        # 每条的地址和结果分两列，手机上也放得下；一行一条不换行
        self.assertIn(".line-row{", self.css)
        self.assertIn(".code-lines{", self.css)
        mobile = self.css.split("@media (max-width:760px)")[1]
        self.assertIn(".line-row{", mobile)

    def test_no_leftover_crlf(self):
        """index.html 统一用 LF：混用换行会让后续按行替换的改动对不上。"""
        with open(os.path.join(UI, "index.html"), "rb") as f:
            self.assertEqual(f.read().count(b"\r\n"), 0)

    def test_static_not_cached(self):
        """升级后手机上还看到旧布局，多半是浏览器抱着旧 app.js。静态文件必须 no-store。"""
        src = read(os.path.join("..", "tmb", "webui.py"))
        self.assertIn("no-store", src)


if __name__ == "__main__":
    unittest.main()
