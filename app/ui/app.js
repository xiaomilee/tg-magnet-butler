/* 磁力管家 · 管理面板 */
(function () {
  "use strict";

  var TOKEN_KEY = "tmb_access_token";
  var token = "";
  try {
    var qs = new URLSearchParams(location.search);
    if (qs.get("token")) { localStorage.setItem(TOKEN_KEY, qs.get("token")); }
    token = localStorage.getItem(TOKEN_KEY) || "";
  } catch (e) { /* ignore */ }

  var $ = function (id) { return document.getElementById(id); };
  var state = {
    rules: [], logsAuto: true, timer: null, tab: "",
    cfg: null,
    /* 点过「最近投递 → 清除」后先别回填：切页/轮询都不该把刚清掉的内容又铺回来 */
    histCleared: false,
    /* 自动侦测出来的网关端口：界面上不放输入框了，保存配置时原样带回去 */
    xFnPort: 5666,
    /* 哪些页还有没保存的改动：手机上最容易「改一半就切走」，切走前问一句 */
    dirty: {},
    /* 每条代理线路的最近结果，键是归一化后的地址；结果列表和概览状态共用这一份 */
    proxyHealth: {},
    /* 老配置里每条线路的启用标记：面板上不显示「启用」了，但别让它保存一次就复活 */
    proxyOn: {},
    /* 正在测速：这时候别让轮询把「…」刷成旧结果 */
    proxyTesting: false
  };

  /* ---------------- 通用 ---------------- */
  function toast(msg, isErr) {
    var el = $("toast");
    el.textContent = msg;
    el.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.className = "toast"; }, 2800);
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    if (token) headers["X-Token"] = token;
    return fetch("api" + path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: "响应解析失败" }; })
        .then(function (data) {
          if (r.status === 401) {
            var t = window.prompt("面板访问口令：", token || "");
            if (t !== null) {
              token = t.trim();
              localStorage.setItem(TOKEN_KEY, token);
              toast("已记录口令，请重试");
            }
            throw new Error("未授权");
          }
          return data;
        });
    });
  }

  /* 二次确认：手机上一指点错的代价比桌面大得多，覆盖配置/删规则这类动作先问一句。
     自己画弹窗而不是用 window.confirm：原生弹窗在 iOS 上样式不可控，按钮还挤在一起。 */
  var modalResolve = null;

  function askConfirm(text, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var ok = $("modalOk");
      $("modalTitle").textContent = opts.title || "确认操作";
      $("modalText").textContent = text;
      ok.textContent = opts.ok || "确定";
      $("modalCancel").textContent = opts.cancel || "取消";
      ok.className = "btn " + (opts.soft ? "" : "danger-solid");
      $("modal").hidden = false;
      modalResolve = resolve;
      setTimeout(function () { try { ok.focus(); } catch (e) { } }, 30);
    });
  }

  function closeModal(result) {
    var m = $("modal");
    if (m.hidden) return;
    m.hidden = true;
    var r = modalResolve;
    modalResolve = null;
    if (r) r(!!result);
  }

  /* 请求进行中把按钮锁住并转圈：既提示「在处理」，也挡住连点造成重复提交 */
  function busy(btn, work) {
    if (!btn) return Promise.resolve(work());
    if (btn.disabled) return Promise.resolve();
    btn.disabled = true;
    btn.classList.add("busy");
    var done = function () { btn.disabled = false; btn.classList.remove("busy"); };
    return Promise.resolve().then(work).then(
      function (v) { done(); return v; },
      function (e) { done(); throw e; });
  }

  /* 只提交改动的那一段配置。整份 config 提交换来的问题是：手机上某页还没加载完就点开关，
     会把别的字段一起写成空值。后端 /api/config 本来就按 patch 合并，用不上整份。 */
  function patchConfig(patch) {
    return api("/config", { method: "POST", body: { config: patch } }).then(function (d) {
      if (!d.ok) throw new Error(d.error || "保存失败");
      if (d.config) { fillConfig(d.config); renderTokenMeta(d.xunlei_auth); }
      return d;
    });
  }

  /* 表格在手机上会被折成一列（见 style.css 的 .table td[data-label]），
     这里按表头自动补 data-label：新增表格不用再手写一遍列名。 */
  function decorateTables() {
    Array.prototype.forEach.call(document.querySelectorAll(".table"), function (t) {
      var heads = Array.prototype.map.call(t.querySelectorAll("thead th"), function (th) {
        return String(th.textContent || "").trim();
      });
      if (!heads.length) return;
      Array.prototype.forEach.call(t.querySelectorAll("tbody tr"), function (tr) {
        if (tr.children.length !== heads.length) return;   // 「暂无记录」那种跨列行不动
        Array.prototype.forEach.call(tr.children, function (td, i) {
          if (!td.getAttribute("data-label")) td.setAttribute("data-label", heads[i]);
        });
      });
    });
  }

  /* 手机抽屉：打开时锁住页面滚动，免得背景跟着滑 */
  function setNav(open) {
    var app = $("app");
    if (!app) return;
    app.classList.toggle("nav-open", !!open);
    $("navShade").hidden = !open;
    $("navToggle").setAttribute("aria-expanded", open ? "true" : "false");
    document.body.style.overflow = open ? "hidden" : "";
  }

  function isNarrow() { return window.matchMedia("(max-width:760px)").matches; }

  function val(id) { var el = $(id); return el ? String(el.value || "").trim() : ""; }
  function num(id, dflt) {
    var v = parseInt(val(id), 10);
    return isNaN(v) ? (dflt || 0) : v;
  }
  function checked(id) { var el = $(id); return !!(el && el.checked); }
  function setVal(id, v) { var el = $(id); if (el) el.value = (v === undefined || v === null) ? "" : v; }
  function setChecked(id, v) { var el = $(id); if (el) el.checked = !!v; }
  /* 读上一次拿到的配置里某个字段，用来保住面板上没有对应控件的项 */
  function oldFilter(key, dflt) {
    var x = state.cfg && state.cfg.download && state.cfg.download.xunlei;
    var f = x && x.file_filter;
    return (f && f[key] !== undefined && f[key] !== null) ? f[key] : dflt;
  }
  /* 文件筛选区的状态提示：关掉时整块变淡并说清「现在一律全下」。
     这块规则对所有投递生效（自动投递 / 按钮补投 / 磁力直链），别让人以为只管某条路。 */
  function syncFilterBox() {
    var on = checked("xFOn"), st = $("xFilterState"), hint = $("xFHint");
    var ids = ["xFVideo", "xFSubtitle", "xFImage", "xFDoc", "xFArchive", "xFOther"];
    var picked = ids.filter(function (id) { return checked(id); }).length;
    ids.forEach(function (id) {
      var el = $(id), box = el && el.closest(".chip-check");
      if (box) box.classList.toggle("on", !!el.checked);
    });
    if ($("xFilterBox")) $("xFilterBox").classList.toggle("off", !on);
    if (st) st.textContent = on ? "生效中：所有投递都按下面规则挑" : "已关闭：多文件种子一律全下";
    if (!hint) return;
    if (!on) {
      hint.textContent = "现在多文件种子整包全下，下面这些设置存着但不生效。"
        + "这也包括关掉自动投递后手动补投、以及直接发磁力链接——它们走的是同一份规则。";
    } else if (!picked) {
      hint.textContent = "一个类别都没勾：规则会把文件全筛掉，那时会自动改回全选（不会投一个空任务出去）。";
    } else {
      var min = num("xFMin", 0);
      hint.textContent = "只下勾选的 " + picked + " 类；"
        + (min > 0 ? "小于 " + min + " MB 的跳过（字幕和说明除外）；" : "")
        + "一个都没留下时会自动改回全选。";
    }
  }
  function listToStr(arr) { return (arr || []).join(","); }
  function strToList(s) {
    return String(s || "").split(/[,，\s]+/).map(function (x) { return x.trim(); })
      .filter(function (x) { return x.length; });
  }
  function esc(s) {
    return String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function dur(sec) {
    sec = Math.max(0, sec | 0);
    var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    if (d) return d + " 天 " + h + " 小时";
    if (h) return h + " 小时 " + m + " 分";
    return m + " 分";
  }
  function ts2str(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return (d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  /* ---------------- 状态 ---------------- */
  function loadStatus() {
    return api("/status").then(function (d) {
      if (!d.ok) return;
      $("brandVersion").textContent = "v" + d.version;
      // 三态要分清：已连上 / 配了但连不上 / 压根没配，别把报错显示成「启动中」
      var botName = d.bot.username ? "@" + d.bot.username
        : (!d.bot.configured ? "未配置" : (d.bot.last_error ? "连接失败" : "启动中"));
      $("statBot").textContent = botName;
      $("statBotSub").textContent = d.bot.running
        ? (d.bot.paused ? "已暂停" : "轮询中")
        : (d.bot.last_error || (d.bot.configured ? "未运行" : "去设置里填 Bot Token"));
      $("statToday").textContent = d.stats.today;
      $("statTotal").textContent = "累计 " + d.stats.success + " / 全部 " + d.stats.total;
      var bn2 = { xunlei: "迅雷", aria2: "aria2", qbittorrent: "qBittorrent", none: "仅回传" };
      $("statBackend").textContent = bn2[d.download.backend] || d.download.backend || "—";
      /* 下载页收起来之后，这格是唯一能看出「迅雷连没连上」的地方：副标题就写连接状态 */
      var xc0 = d.xunlei_conn || {};
      var fb0 = (d.download.fallback || []).join(" → ");
      var bsub = $("statBackendSub");
      bsub.textContent = d.download.backend === "xunlei"
        ? xlStateText(xc0)
        : "兜底 " + (fb0 || "无");
      bsub.className = "stat-sub" + (d.download.backend === "xunlei" && !xc0.ok ? " warn-text" : "");
      bsub.title = [xlStateText(xc0), fb0 ? "兜底 " + fb0 : "", xc0.text || ""]
        .filter(function (s) { return s; }).join("　");
      $("statRules").textContent = d.rules_count;
      $("statUptime").textContent = "运行 " + dur(d.uptime);
      var bn = { xunlei: "迅雷", aria2: "aria2", qbittorrent: "qBittorrent", none: "仅回传" };
      // 现在只有一种接法（飞牛官方套件），顶栏就把「连没连上」一起写出来
      $("pillBackend").textContent = "后端 " + (bn[d.download.backend] || d.download.backend || "—") +
        (d.download.backend === "xunlei" ? " · " + (xc0.ok ? "已连接" : "未连接") : "");
      $("pillBackend").title = xc0.text || "";
      var xc = xc0, xcs = $("xConnState");
      if (xcs) {
        xcs.textContent = (xc.port || xc.ok) ? xlStateText(xc) : "连接状态：启动时自动侦测";
        xcs.className = "small " + (xc.ok ? "muted" : (xc.text ? "warn-text" : "muted"));
        xcs.title = xc.text || "";
      }
      syncQuickXunlei(xc);

      /* 代理线路：只写「最近真的走通了哪条」，没走过就写有几条线路——
         别把「配了但没连过」写成「正在用」，那会让人以为代理是好的。
         线路之间互为备胎，所以这里只关心「现在在走谁」。 */
      var px = d.proxy || {}, pcs = $("pcState");
      var plines = px.lines || [];
      /* 每条线路的最近状态：后端直接给，不用先点一次测速。
         顺手存起来，设置页那个列表要画同一份数据。 */
      var phealth = {};
      plines.forEach(function (l) {
        if (l.url) phealth[normUrl(l.url)] = l;
      });
      state.proxyHealth = phealth;
      if (pcs) {
        var kind = px.preferred_kind || "", pref = px.preferred || "";
        /* 「能用但不完整」的那条会带 note（例：检索站不通）。它不影响走这条线，
           但得让人一眼看见——不然检索失败时会去怀疑代理整个挂了。 */
        var hit = plines.filter(function (l) { return l.url === pref; })[0] || {};
        var pnote = hit.note || "", addr = shortAddr(pref);
        if (kind === "direct") {
          pcs.textContent = "直连" + (px.configured ? "（线路不通）" : "");
        } else if (pref) {
          pcs.textContent = "走 " + addr + (px.auto === false ? "（自动切换已关）" : "");
        } else if (plines.length) {
          pcs.textContent = plines.length + " 条线路 · 还没测";
        } else {
          pcs.textContent = "直连";
        }
        if (pnote && kind !== "direct") pcs.textContent += " · " + pnote;
        // 只有「线路都挂了、已经在直连」才标橙：那是有事要处理的状态
        pcs.className = "small " + (kind === "direct" && px.configured ? "warn-text" : "muted");
        pcs.title = plines.map(function (l) {
          return shortAddr(l.url)
            + (l.ok === true ? " ✓" + (l.ms ? " " + l.ms + "ms" : "")
              : (l.ok === false ? " ✗" + (l.error || "不通") : " 未测"))
            + (l.ok === true && l.note ? "（" + l.note + "）" : "");
        }).concat([
          !plines.length ? "一条都没填 = 直连" : "",
          pnote ? pnote + "（这条线路本身能连，只是那个站被代理/节点拒了）" : "",
          px.auto === false ? "自动切换已关" : "互为备胎：还通就继续用，断了才换（每 3 分钟测一次）"
        ]).filter(function (s) { return s; }).join("\n");
        // 设置页那个结果列表跟着刷新；正在测 / 有未保存改动时不抢
        if (!state.proxyTesting && !state.dirty.settings) {
          paintLineResults(readProxyLines(), phealth);
        }
      }

      /* 迅雷令牌：pan_auth 是 JWT，剩余天数从里面解出来。快到期才挂胶囊，平时不占地方 */
      var tk = d.xunlei_token || {}, tp = $("pillToken");
      if (tk.ok && (tk.level === "soon" || tk.level === "expired")) {
        tp.hidden = false;
        tp.className = "pill " + (tk.level === "expired" ? "err" : "warn");
        tp.textContent = "迅雷令牌 " + (tk.text || "快到期");
        tp.title = tk.level === "expired"
          ? "令牌已过期，投递会失败。点这里让程序去本机重抓；抓不到就去迅雷套件确认账号仍在线"
          : "点这里让程序现在就去本机捞一次更新的令牌（迅雷套件续期后才有得换）";
      } else {
        tp.hidden = true;
        tp.textContent = "";
      }

      var dot = $("botDot"), label = $("botLabel");
      /* 409 轮询冲突：这不是网络问题，是同一串 Token 还有别处在收消息。
         面板上得能直接看出来，否则只会反复看到「连接失败」 */
      var conflict = d.bot.poll_state === "conflict";
      if (conflict) { dot.className = "dot err"; label.textContent = "轮询冲突"; }
      else if (d.bot.running) { dot.className = "dot on"; label.textContent = "@" + (d.bot.username || "bot") + (d.bot.paused ? " · 暂停" : ""); }
      else if (d.bot.configured) { dot.className = "dot err"; label.textContent = "连接失败"; }
      else { dot.className = "dot"; label.textContent = "未配置"; }
      var hint = d.bot.poll_hint || d.bot.last_error || "";
      $("sideHint").textContent = hint ? String(hint).slice(0, 60) : "Telegram 机器人";
      if (conflict) { $("sideHint").className = "hint err"; }
      else { $("sideHint").className = "hint"; }
      state.bot = d.bot;
      syncQuickBot(d.bot);
      return d;
    }).catch(function () { });
  }

  /* 概览页的快捷开关：机器人状态与暂停按钮。手机上不用翻到设置页就能停掉投递。 */
  function syncQuickBot(bot) {
    bot = bot || {};
    var btn = $("qBotPause");
    if (!btn) return;
    var st = $("qBotState");
    if (!bot.configured) st.textContent = "未配置 token";
    else if (bot.poll_state === "conflict") st.textContent = "轮询冲突";
    else if (!bot.running) st.textContent = "未运行";
    else st.textContent = bot.paused ? "已暂停接收" : "轮询中";
    btn.textContent = bot.paused ? "恢复" : "暂停";
    btn.disabled = !bot.configured;
    var sp = $("sidePause");
    if (sp) { sp.textContent = bot.paused ? "恢复机器人" : "暂停机器人"; sp.disabled = !bot.configured; }
    var qh = $("quickHint");
    if (qh) {
      var tip = bot.poll_hint || "";
      qh.textContent = tip;
      qh.className = "small " + (bot.poll_state === "conflict" ? "hint-err" : "muted");
    }
  }

  /* 迅雷连接状态只有三种说法：连上了、断了、还没试过。各处（卡片/顶栏/快捷开关）都用这一句，
     免得同一个状态在页面上写出三种措辞。 */
  function xlStateText(xc) {
    xc = xc || {};
    if (xc.ok) return "已连接 · 网关 :" + (xc.port || "?");
    if (xc.port) return "未连上 · 网关 :" + xc.port;
    return "还没连上";
  }

  /* 概览页「迅雷」那一格：状态字 + 重连按钮。下载页收起来之后，这里是唯一的入口。 */
  function syncQuickXunlei(xc) {
    var st = $("qXlState"), btn = $("qXlReconnect");
    if (!st) return;
    xc = xc || {};
    if (xc.ok) {
      st.textContent = "已连接 :" + (xc.port || "?");
      st.className = "ok-text";
    } else if (xc.port) {
      st.textContent = "未连上 :" + xc.port;
      st.className = "warn-text";
    } else {
      st.textContent = "点右边重连";
      st.className = "warn-text";
    }
    st.title = xc.text || "";
    if (btn) btn.disabled = false;
  }

  /* 暂停/恢复只影响「收消息」，不影响已经在跑的任务；状态在内存里，重启应用会恢复成不暂停 */
  function togglePause(btn) {
    var cur = !!(state.bot && state.bot.paused);
    busy(btn, function () {
      return api("/bot/pause", { method: "POST", body: { paused: !cur } }).then(function (d) {
        if (!d.ok) throw new Error(d.error || "操作失败");
        state.bot = state.bot || {};
        state.bot.paused = !!d.paused;
        syncQuickBot(state.bot);
        toast(d.paused ? "已暂停：机器人只回一句提示，不再处理消息" : "已恢复接收消息");
      }).catch(function (e) { toast(e.message, true); });
    });
  }

  /* 哪些输入框不该算「未保存的改动」：搜索/筛选这类只是查询条件，不落盘 */
  var NO_DIRTY = { sQuery: 1, sCategory: 1, sSort: 1, sSeeders: 1, sLimit: 1,
    libQuery: 1, libFilter: 1, qAuto: 1, qLibCheck: 1, qLibSkip: 1 };

  function markDirty(e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var sec = t.closest(".tab");
    if (!sec) return;
    var tab = sec.id.replace(/^tab-/, "");
    if (tab === "overview" || tab === "logs" || tab === "search") return;
    if (t.id && NO_DIRTY[t.id]) return;
    // 规则卡片的输入框只有 class 没有 id，按所在页算
    if (!t.id && tab !== "rules") return;
    if (state.dirty[tab]) return;
    state.dirty[tab] = true;
    updateSaveBar();
  }

  var SAVE_LABEL = { rules: "规则", download: "下载设置", settings: "设置", library: "历史字典" };

  /* 「还有改动没保存」提示条：表单长的时候保存按钮会滚出屏幕，这里给个随身的 */
  function updateSaveBar() {
    var bar = $("saveBar");
    if (!bar) return;
    var tab = state.tab, dirty = !!(tab && state.dirty[tab]);
    bar.hidden = !dirty;
    bar.classList.toggle("show", dirty);
    document.body.classList.toggle("has-save", dirty);
    if (dirty) {
      $("saveBarText").textContent = "「" + (SAVE_LABEL[tab] || tab) + "」还有改动没保存";
      $("saveBarSave").textContent = tab === "rules" ? "保存规则" : "保存";
    }
  }

  /* force=true 时无视「刚清除过」——顶栏「刷新」用的就是它，一按就重新载回来。
     普通刷新（切回概览、8 秒轮询）若用户刚点过清除，就别把表又铺回去。 */
  function loadHistory(force) {
    if (state.histCleared && !force) return Promise.resolve();
    return api("/history?limit=20").then(function (d) {
      var tb = $("historyTable").querySelector("tbody");
      state.histCleared = false;
      $("historyHint").textContent = "";
      if (!d.ok || !d.items.length) {
        tb.innerHTML = '<tr class="empty-row"><td colspan="7" class="empty">暂无记录</td></tr>';
        syncPicked();
        return;
      }
      tb.innerHTML = d.items.map(function (it) {
        var cls = it.status === "ok" || it.status === "queued" ? "ok" : (it.status === "skipped_dup" ? "neutral" : "err");
        var label = { ok: "已投递", queued: "排队", skipped_dup: "已去重", failed: "失败" }[it.status] || it.status;
        var mg = histMagnet(it);
        return "<tr>" +
          '<td class="pick"><input type="checkbox" class="hpick"' + (mg ? ' data-magnet="' + esc(mg) + '"' : " disabled") +
          ' aria-label="选中这条" title="' + (mg ? "选中后可以一次复制" : "这条记录没有磁力链接") + '"></td>' +
          "<td>" + esc(ts2str(it.ts)) + "</td>" +
          '<td><span class="ellipsis" title="' + esc(it.title) + '">' + esc(it.title) + "</span></td>" +
          "<td>" + esc(it.backend || "—") + "</td>" +
          "<td>" + esc(it.task_id || "—") + "</td>" +
          '<td><span class="tag ' + cls + '">' + esc(label) + "</span></td>" +
          '<td class="copy-col"><button class="btn ghost sm hcopy"' + (mg ? ' data-magnet="' + esc(mg) + '"' : " disabled") +
          ' title="' + (mg ? "复制这条磁力链接" : "这条记录没有磁力链接") + '">复制</button></td></tr>';
      }).join("");
      decorateTables();
      syncPicked();
    }).catch(function () { });
  }

  /* 一条投递记录对应的磁力：老记录可能只留了 infohash（比如从种子文件投的），
     那就拼一条标准 magnet 出来，照样能拿去迅雷里加。 */
  function histMagnet(it) {
    var m = String(it.magnet || "").trim();
    if (m) return m;
    var h = String(it.infohash || "").trim();
    return h ? "magnet:?xt=urn:btih:" + h : "";
  }

  function pickedMagnets() {
    return Array.prototype.map.call(
      document.querySelectorAll("#historyTable .hpick:checked"),
      function (el) { return el.getAttribute("data-magnet"); }
    ).filter(function (s) { return s; });
  }

  /* 有磁力、能勾的那些（失败的记录也可能没有磁力，别让「全选」把它们算进去） */
  function pickableBoxes() {
    return Array.prototype.filter.call(
      document.querySelectorAll("#historyTable .hpick"),
      function (b) { return !b.disabled; });
  }

  /* 勾选状态变了：按钮文案跟着变（写上几条，省得再数一遍），「全选」也跟着换说法 */
  function syncPicked() {
    var btn = $("btnHistCopySel"), all = $("btnHistAll");
    if (!btn) return;
    var boxes = pickableBoxes();
    var n = pickedMagnets().length;
    btn.textContent = n ? "复制 " + n + " 条" : "复制所选";
    btn.disabled = !n;
    if (!all) return;
    all.disabled = !boxes.length;
    var allOn = boxes.length > 0 && pickedMagnets().length === boxes.length;
    all.textContent = allOn ? "取消全选" : "全选";
  }

  function setAll(on) {
    Array.prototype.forEach.call(pickableBoxes(), function (b) { b.checked = !!on; });
    syncPicked();
  }

  function copyPicked() {
    var arr = pickedMagnets();
    if (!arr.length) return toast("先勾选几条");
    copyText(arr.join("\n")).then(function (ok) {
      if (ok) toast("已复制 " + arr.length + " 条磁力，一行一条");
      else toast("复制失败，请在表格里长按标题手动复制", true);
    });
  }

  /* 「最近投递」的清除：只清屏，数据库里一条都不删——列表本身就是查询结果，
     真要删记录那是另一个危险动作，这里不碰。清完到下次手动刷新前不再回填，
     否则切一下页面又铺回来，看着像没生效。 */
  function clearHistory() {
    state.histCleared = true;
    $("historyTable").querySelector("tbody").innerHTML =
      '<tr class="empty-row"><td colspan="7" class="empty">已清除（只是清屏，数据库里的投递记录都还在）</td></tr>';
    $("historyHint").textContent = "已清除 · 刷新可重载";
    syncPicked();
    toast("已清除这张表；记录还在，点「刷新」能再看到");
  }

  /* ---------------- 配置 ---------------- */
  function fillConfig(c) {
    var t = c.telegram || {}, n = c.nyaa || {}, a = c.app || {};
    var dl = c.download || {}, x = dl.xunlei || {}, ar = dl.aria2 || {}, q = dl.qbittorrent || {};
    setVal("tToken", t.bot_token); setVal("tChats", listToStr(t.allowed_chat_ids));
    setVal("tAdmins", listToStr(t.admin_user_ids));
    setVal("tPoll", t.poll_timeout);
    setVal("nBase", n.base_url); setVal("nTimeout", n.timeout);
    /* 代理就一个框：第一行是老的 telegram.proxy，后面几行是通道表。
       老配置里单填的检索代理也并进来——不然那些用户会以为地址丢了。 */
    var plines = [];
    if (t.proxy) plines.push(t.proxy);
    (c.proxy_channels || []).forEach(function (ch) {
      if (ch && ch.url) plines.push(ch.url);
    });
    if (n.proxy && plines.indexOf(n.proxy) < 0) plines.push(n.proxy);
    state.proxyOn = {};
    (c.proxy_channels || []).forEach(function (ch) {
      if (ch && ch.url) state.proxyOn[normUrl(ch.url)] = ch.enabled !== false;
    });
    setProxyLines(plines);
    setChecked("pcAuto", c.proxy_auto !== false);
    setVal("cDedupe", a.dedupe_days); setVal("cLogLevel", a.log_level); setVal("cToken", a.access_token);
    setVal("dBackend", dl.backend); setVal("dFallback", listToStr(dl.fallback));
    setChecked("dAuto", dl.auto_download); setVal("dAutoPick", dl.auto_pick);
    state.xFnPort = x.fnos_port || 5666;
    setVal("xPanAuth", x.pan_auth);
    setVal("xFnToken", x.fnos_cookie || x.fnos_token); setVal("xTarget", x.target);
    setVal("xFolder", x.parent_folder_id); setChecked("xFOn", x.select_all_files !== true);
    var ff = x.file_filter || {};
    setChecked("xFVideo", ff.video !== false); setChecked("xFSubtitle", ff.subtitle !== false);
    setChecked("xFImage", ff.image === true); setChecked("xFDoc", ff.doc === true);
    setChecked("xFArchive", ff.archive === true); setChecked("xFOther", ff.other === true);
    setVal("xFMin", ff.min_size_mb === undefined ? 100 : ff.min_size_mb);
    setVal("xFMax", ff.max_size_mb === undefined ? 0 : ff.max_size_mb);
    setVal("xFInclude", ff.name_include); setVal("xFExclude", ff.name_exclude);
    setChecked("xFSubExempt", ff.subtitle_exempt_size !== false);
    syncFilterBox();
    setChecked("xAutoRefresh", x.auto_refresh_token !== false);
    setChecked("xFbFolder", x.auto_fallback_folder !== false);
    setVal("aUrl", ar.rpc_url); setVal("aSecret", ar.secret); setVal("aDir", ar.dir);
    setVal("qUrl", q.url); setVal("qUser", q.username); setVal("qPass", q.password);
    setVal("qPath", q.save_path); setVal("qCat", q.category);
    setVal("qTags", q.tags); setChecked("qPaused", q.paused); setVal("qProxy", q.proxy);
    var lib = c.library || {};
    setVal("libRoots", (lib.roots || []).join("\n"));
    setVal("libDepth", lib.max_depth); setVal("libMax", lib.max_items);
    setChecked("libFiles", lib.include_files); setChecked("libCheck", lib.check_search);
    setChecked("libSkip", lib.skip_download);
    setChecked("libSkipDeleted", lib.skip_deleted);
    $("libPolicy").textContent = (lib.check_search === false) ? "只记录不拦截"
      : libPolicyText(lib.skip_download !== false, lib.skip_deleted !== false);
    state.cfg = c;
    syncQuick();
  }

  /* 快捷开关跟着配置走：默认值都是「开」，只有明确关掉时才不勾 */
  function syncQuick() {
    var c = state.cfg || {}, dl = c.download || {}, lib = c.library || {};
    setChecked("qAuto", dl.auto_download !== false);
    setChecked("qLibCheck", lib.check_search !== false);
    setChecked("qLibSkip", lib.skip_download !== false);
    /* 没填监控目录时这两个开关一条都不会生效（library_check 直接返回 None），
       与其让人以为「开了却没拦」，不如先置灰并写清缺什么 */
    var noRoots = !(lib.roots && lib.roots.length);
    markNoRoots("qLibCheck", noRoots, noRoots ? "先到「历史字典」填监控目录" : "下过的先提醒你");
    markNoRoots("qLibSkip", noRoots, noRoots ? "先到「历史字典」填监控目录" : "命中就不重复投递");
  }

  function markNoRoots(id, off, tip) {
    var el = $(id);
    if (!el) return;
    var box = el.closest(".switch-field");
    el.disabled = !!off;
    if (box) {
      box.classList.toggle("off", !!off);
      box.title = off ? "还没填监控目录，这两个开关现在不管用" : "";
    }
    var em = box && box.querySelector("em");
    if (em && tip) em.textContent = tip;
  }

  function libPolicyText(skipPresent, skipDeleted) {
    if (skipPresent && skipDeleted) return "提示 + 存在/已删都跳过投递";
    if (skipPresent) return "提示 + 只有「已存在」跳过投递";
    if (skipDeleted) return "提示 + 只有「曾存在已删」跳过投递";
    return "提示但仍投递";
  }

  function loadConfig() {
    return api("/config").then(function (d) {
      if (d.ok) { fillConfig(d.config); renderTokenMeta(d.xunlei_auth); }
    }).catch(function () { });
  }

  function fmtAge(sec) {
    if (sec === null || sec === undefined) return "";
    if (sec < 120) return "刚更新";
    if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
    if (sec < 86400) return Math.floor(sec / 3600) + " 小时前";
    return Math.floor(sec / 86400) + " 天前";
  }

  /* 面板上「手上这串 fnos-token 算不算数」的唯一依据：来源 + 多久了 + 这次有没有读到新的 */
  function renderTokenMeta(a) {
    var el = $("xTokenMeta");
    if (!el) return;
    if (!a) { el.textContent = ""; return; }
    if ((a.mode || "") !== "fnos_gateway") {
      el.textContent = "当前是面板模式，只用到 pan_auth，不需要 fnos-token。";
      return;
    }
    var src = { browser: "浏览器登录态", manual: "手动填写", detect: "自动侦测" }[a.source] || "";
    var parts = [];
    if (a.has_token) {
      var when = fmtAge(a.age);
      parts.push("已存 fnos-token" + (src || when ? "（" + [src, when].filter(Boolean).join("，") + "）" : ""));
    } else {
      parts.push("还没存 fnos-token");
    }
    parts.push(a.browser_cookie_seen
      ? "本次已从浏览器读到最新登录态，会自动存下"
      : "本次没读到浏览器登录态：本面板多半不是从飞牛桌面打开的（跨端口不共享 Cookie），重开一次，或手动粘贴");
    el.textContent = "令牌：" + parts.join("；");
  }

  /* ---------------- 代理线路 ----------------
     一个框，一行一个地址，从上往下试。面板只管「有哪些地址」，
     这次走哪条由后端按健康度决定——所以这里不排序、不分主备、不拖拽。 */
  function readProxyLines() {
    var box = $("pcAll");
    if (!box) return [];
    return String(box.value || "").split(/\r?\n/)
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
  }

  function setProxyLines(lines) {
    var box = $("pcAll");
    if (!box) return;
    var list = (lines || []).filter(function (u) { return String(u || "").trim(); });
    box.value = list.join("\n");
    syncProxyCount();
    paintLineResults(list, state.proxyHealth || {});
  }

  function syncProxyCount() {
    var el = $("pcCount");
    if (!el) return;
    var lines = readProxyLines();
    if (!lines.length) { el.textContent = "一行一条，空着就是直连"; return; }
    var bad = lines.filter(function (u) { return lineIssue(u); }).length;
    el.textContent = "共 " + lines.length + " 条，从上往下试"
      + (bad ? "（" + bad + " 条格式不对）" : "");
  }

  /* 地址只留 host:port：一行里要同时放地址和结果，整个 URL 在手机上也放不下 */
  function shortAddr(u) {
    var s = String(u || "").trim();
    if (!s) return "直连";
    return (s.replace(/^[a-z0-9+.-]+:\/\//i, "").replace(/\/.*$/, "")
      .split("@").pop()) || s;
  }

  /* 报错也压短：地址在左边已经写了，再说一遍「经代理 http://…
     拒绝连接（errno 111）」只会在手机上被省略号吃掉。完整原因挂在 title 上。 */
  function shortErr(e) {
    var s = String(e || "").replace(/^经代理\s+\S+\s*/, "");
    var cut = s.indexOf("。");
    if (cut > 0) s = s.slice(0, cut);
    return s || "不通";
  }

  /* 每条的实测结果。地址已经在上面的框里了，这里再写一遍是为了
     「第几条 = 哪个地址」一眼对得上；一行一条、不换行，省得手机上为了看状态来回滚。 */
  /* 地址写错的地方当场说：后端认不出来会把它整条丢掉，用户只会以为「填了没生效」 */
  function lineIssue(u) {
    var s = String(u || "").trim();
    if (/^socks/i.test(s)) return "只支持 http 端口（Clash 的 mixed 端口换个前缀）";
    var host = s.replace(/^[a-z0-9+.-]+:\/\//i, "").replace(/\/.*$/, "").split("@").pop();
    if (!/:\d{1,5}$/.test(host)) return "格式不对，要写成 http://主机:端口";
    return "";
  }

  function paintLineResults(lines, health) {
    var box = $("pcList");
    if (!box) return;
    var list = lines || [];
    if (!list.length) {
      box.innerHTML = '<p class="empty">没填线路 = 直连，请求不经过任何代理。</p>';
      return;
    }
    box.innerHTML = list.map(function (u, i) {
      var r = (health || {})[normUrl(u)] || {};
      // 部分可用（线路通、某个站不通）用琥珀色，别用红色：红的看着像这条线废了
      var note = r.note || "", issue = lineIssue(u);
      var cls = "ch-res", txt = "未测", title = "";
      if (issue) {
        txt = "✗ " + issue;
        cls += " warn-text";
        title = "这一行不会被当成线路：" + issue;
      } else if (r.ok === true) {
        txt = "✓ " + (r.ms === null || r.ms === undefined ? "通" : r.ms + " ms")
          + (note ? " · " + note + "✗" : "");
        cls += note ? " partial-text" : " ok-text";
        title = note ? note + (r.note_detail ? "：" + r.note_detail : "") : "";
      } else if (r.ok === false) {
        txt = "✗ " + shortErr(r.error);
        cls += " warn-text";
        title = r.error || "";
      }
      return '<div class="line-row" data-url="' + esc(u) + '">'
        + '<span class="ch-idx">' + (i + 1) + '</span>'
        + '<code class="ch-addr">' + esc(shortAddr(u)) + '</code>'
        + '<span class="' + cls + '" title="' + esc(title) + '">' + esc(txt) + '</span>'
        + '</div>';
    }).join("");
  }

  function dirtySettings() {
    state.dirty.settings = true;
    updateSaveBar();
  }

  /* 把表单地址收拾成和后端一样的样子：只用来「哪一行对应哪条结果」的比对 */
  function normUrl(u) {
    u = String(u || "").trim();
    if (!u) return "";
    if (u.indexOf("://") < 0) u = "http://" + u;
    return u.toLowerCase();
  }

  /* 「测速并切换」：框里的每条 + 直连一起测（后端并行，一个超时窗口出结果），
     然后按「还在用就继续、断了才换」定走向——线路之间互为备胎。
     表单里有没保存的改动会先落盘——不然选中的那条线，程序根本不知道它存在。 */
  function testProxyLines() {
    var btn = $("btnPcTest"), st = $("pcState"), out = $("pcTestOut");
    var needSave = !!state.dirty.settings;

    btn.disabled = true; btn.textContent = "测试中…";
    st.textContent = "逐条测线路…"; st.className = "small muted";
    out.textContent = ""; out.className = "ch-res pc-out";
    Array.prototype.forEach.call(document.querySelectorAll("#pcList .line-row .ch-res"), function (el) {
      el.textContent = "…"; el.className = "ch-res muted";
    });
    state.proxyTesting = true;

    (needSave ? saveConfig() : Promise.resolve()).catch(function () { /* 存不下也照样测 */ })
      .then(function () {
        return api("/proxy/test", { method: "POST", body: { lines: readProxyLines() } });
      }).then(function (d) {
        /* 保存会重画输入框（saveConfig → fillConfig），所以这里重新读一遍地址，
           再把结果按地址配回去——不然结果全画在已经扔掉的 DOM 上 */
        var lines = readProxyLines(), health = {}, direct = null;
        (d.lines || []).forEach(function (l) {
          if (l.kind === "direct") { direct = l; return; }
          if (l.url) health[normUrl(l.url)] = l;
        });
        state.proxyHealth = health;
        state.proxyTesting = false;
        paintLineResults(lines, health);
        out.textContent = direct
          ? ("直连：" + (direct.ok ? "可用" + (direct.ms ? "（" + direct.ms + " ms）" : "") : "不通"))
          : "";
        st.textContent = d.text || "测速完成";
        st.className = "small " + (d.ok ? "muted" : "warn-text");
        toast(d.text || "测速完成", !d.ok);
      }).catch(function (e) {
        state.proxyTesting = false;
        st.textContent = "测试失败：" + e.message; st.className = "small warn-text";
        toast(e.message, true);
      }).then(function () {
        btn.disabled = false; btn.textContent = "测速并切换";
      });
  }

  function collectConfig() {
    /* 一个框里的几行：第一行落在 telegram.proxy（兼容老配置与各处读它的地方），
       后面几行进 proxy_channels。原来「启用」勾选没了（要停用就删掉那一行），
       但老配置里被关掉的那条不要因为保存一次又活过来。 */
    var plines = readProxyLines();
    var wasOn = state.proxyOn || {};
    var chans = plines.slice(1).map(function (u) {
      return { url: u, enabled: wasOn[normUrl(u)] !== false };
    });
    return {
      proxy_channels: chans,
      proxy_auto: checked("pcAuto"),
      telegram: {
        bot_token: val("tToken"), allowed_chat_ids: strToList(val("tChats")),
        admin_user_ids: strToList(val("tAdmins")), proxy: plines[0] || "",
        poll_timeout: num("tPoll", 50)
      },
      // 检索代理已并入上面那一串线路，这里永远留空（= 跟线路表走）
      nyaa: { base_url: val("nBase"), timeout: num("nTimeout", 25), proxy: "" },
      app: {
        dedupe_days: num("cDedupe", 30), log_level: val("cLogLevel"),
        access_token: val("cToken")
      },
      library: {
        roots: String(val("libRoots") || "").split(/\r?\n/).map(function (x) {
          return x.trim();
        }).filter(function (x) { return x.length; }),
        max_depth: num("libDepth", 3), max_items: num("libMax", 20000),
        include_files: checked("libFiles"), check_search: checked("libCheck"),
        skip_download: checked("libSkip"), skip_deleted: checked("libSkipDeleted")
      },
      download: {
        backend: val("dBackend"), fallback: strToList(val("dFallback")),
        auto_download: checked("dAuto"), auto_pick: num("dAutoPick", 1),
        xunlei: {
          mode: "fnos_gateway", host: "127.0.0.1",
          fnos_port: state.xFnPort || 5666, pan_auth: val("xPanAuth"),
          fnos_cookie: val("xFnToken"), target: val("xTarget"),
          parent_folder_id: val("xFolder"), select_all_files: !checked("xFOn"),
          file_filter: {
            video: checked("xFVideo"), subtitle: checked("xFSubtitle"),
            image: checked("xFImage"), doc: checked("xFDoc"),
            archive: checked("xFArchive"), other: checked("xFOther"),
            min_size_mb: num("xFMin", 0), max_size_mb: num("xFMax", 0),
            name_include: val("xFInclude"), name_exclude: val("xFExclude"),
            subtitle_exempt_size: checked("xFSubExempt"),
            /* 面板上没做开关：这是「筛空时宁可不投」的逃生阀，手改配置才用得到，
               这里原样带回去，别因为保存一次就被重置成 true */
            fallback_all: oldFilter("fallback_all", true) !== false
          },
          auto_refresh_token: checked("xAutoRefresh"),
          auto_fallback_folder: checked("xFbFolder")
        },
        aria2: { rpc_url: val("aUrl"), secret: val("aSecret"), dir: val("aDir") },
        qbittorrent: {
          url: val("qUrl"), username: val("qUser"), password: val("qPass"),
          save_path: val("qPath"), category: val("qCat"), tags: val("qTags"),
          paused: checked("qPaused"), proxy: val("qProxy")
        }
      }
    };
  }

  function saveConfig() {
    return api("/config", { method: "POST", body: { config: collectConfig() } })
      .then(function (d) {
        if (!d.ok) throw new Error(d.error || "保存失败");
        fillConfig(d.config);
        renderTokenMeta(d.xunlei_auth);
        state.dirty = {};           // 整份配置都提交了，各页都不再算「未保存」
        updateSaveBar();
        toast("配置已保存");
        loadStatus();
      });
  }

  /* ---------------- 规则 ---------------- */
  function loadRules() {
    return api("/rules").then(function (d) {
      if (!d.ok) return;
      state.rules = d.rules || { rules: [] };
      renderRules();
    }).catch(function () { });
  }

  var CATS = [["0_0", "全部"], ["1_1", "Art - Anime"], ["1_2", "Art - Doujinshi"],
    ["1_3", "Art - Games"], ["1_4", "Art - Manga"], ["1_5", "Art - Pictures"],
    ["2_1", "Real Life - Pictures"], ["2_2", "Real Life - Videos"]];
  var SORTS = [["seeders", "做种数"], ["downloads", "下载量"], ["date", "时间"], ["size", "体积"]];

  function options(list, cur) {
    return list.map(function (p) {
      return '<option value="' + p[0] + '"' + (p[0] === cur ? " selected" : "") + ">" + p[1] + "</option>";
    }).join("");
  }

  function renderRules() {
    var list = (state.rules.rules || []);
    var box = $("rulesList");
    if (!list.length) {
      box.innerHTML = '<p class="muted small">还没有规则，点右上角「新增规则」。</p>';
      return;
    }
    box.innerHTML = list.map(function (r, i) {
      var words = (r.match && r.match.any) || [];
      return '<div class="rule' + (r.enabled === false ? " off" : "") + '" data-i="' + i + '">' +
        '<div class="rule-head"><strong>' + esc(r.name || r.id || "规则" + (i + 1)) + "</strong>" +
        '<label class="chk"><input type="checkbox" class="r-enabled"' + (r.enabled !== false ? " checked" : "") + ">启用</label></div>" +
        '<div class="mini"><span>名称</span><input class="r-name" value="' + esc(r.name || "") + '"></div>' +
        '<div class="mini"><span>命中词（逗号分隔，中文子串匹配）</span><input class="r-any" value="' + esc(words.join(",")) + '"></div>' +
        '<div class="two">' +
        '<div class="mini"><span>分类</span><select class="r-cat">' + options(CATS, r.category || "0_0") + "</select></div>" +
        '<div class="mini"><span>排序</span><select class="r-sort">' + options(SORTS, r.sort || "seeders") + "</select></div>" +
        "</div>" +
        '<div class="two">' +
        '<div class="mini"><span>最少做种</span><input class="r-seed" type="number" min="0" value="' + (r.min_seeders || 0) + '"></div>' +
        '<div class="mini"><span>取回条数</span><input class="r-limit" type="number" min="1" max="50" value="' + (r.limit || 5) + '"></div>' +
        "</div>" +
        '<div class="two">' +
        '<div class="mini"><span>体积下限 MB</span><input class="r-minsz" type="number" min="0" value="' + (r.min_size_mb || 0) + '"></div>' +
        '<div class="mini"><span>体积上限 MB</span><input class="r-maxsz" type="number" min="0" value="' + (r.max_size_mb || 0) + '"></div>' +
        "</div>" +
        '<div class="mini"><span>偏好词（加分，逗号分隔）</span><input class="r-prefer" value="' + esc((r.prefer_words || []).join(",")) + '"></div>' +
        '<div class="mini"><span>排除词（命中即丢弃）</span><input class="r-exclude" value="' + esc((r.exclude_words || []).join(",")) + '"></div>' +
        '<div class="mini"><span>查询模板 <code>{keywords}</code> / <code>{raw}</code></span><input class="r-tpl" value="' + esc(r.query_template || "{keywords}") + '"></div>' +
        '<div class="foot">' +
        '<label class="chk"><input type="checkbox" class="r-auto"' + (r.auto_download !== false ? " checked" : "") + ">自动投递</label>" +
        '<span class="chk">第 <input class="r-pick" type="number" min="1" max="10" style="width:56px" value="' + (r.pick || 1) + '"> 名</span>' +
        '<button class="btn danger sm r-del">删除</button>' +
        "</div></div>";
    }).join("");

    Array.prototype.forEach.call(box.querySelectorAll(".r-del"), function (btn) {
      btn.addEventListener("click", function () {
        var card = btn.closest(".rule");
        var i = parseInt(card.getAttribute("data-i"), 10);
        var name = (state.rules.rules[i] || {}).name || ("第 " + (i + 1) + " 条");
        // 规则一删就整块消失，手机上极易误触；问一句，顺带说清「还没落盘」
        askConfirm("删除规则「" + name + "」？只要不点保存，刷新一下就能恢复。",
          { title: "删除规则", ok: "删除", soft: false }).then(function (yes) {
            if (!yes) return;
            // 先把其他卡片的输入收进来，否则重绘会把「还没保存的编辑」一起丢掉
            state.rules = collectRules();
            state.rules.rules.splice(i, 1);
            renderRules();
            state.dirty.rules = true;
            toast("已移除，记得点保存");
          });
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll(".r-enabled"), function (c) {
      c.addEventListener("change", function () { c.closest(".rule").classList.toggle("off", !c.checked); });
    });
  }

  function collectRules() {
    var cards = document.querySelectorAll("#rulesList .rule");
    var list = [];
    Array.prototype.forEach.call(cards, function (card) {
      var i = parseInt(card.getAttribute("data-i"), 10);
      var old = (state.rules.rules || [])[i] || {};
      var g = function (sel) { return card.querySelector(sel); };
      var rule = JSON.parse(JSON.stringify(old));
      rule.id = old.id || ("rule_" + Math.random().toString(36).slice(2, 7));
      rule.name = g(".r-name").value.trim() || rule.id;
      rule.enabled = g(".r-enabled").checked;
      rule.match = rule.match || {};
      rule.match.any = g(".r-any").value.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean);
      rule.category = g(".r-cat").value;
      rule.sort = g(".r-sort").value;
      rule.min_seeders = parseInt(g(".r-seed").value, 10) || 0;
      rule.limit = parseInt(g(".r-limit").value, 10) || 5;
      rule.min_size_mb = parseInt(g(".r-minsz").value, 10) || 0;
      rule.max_size_mb = parseInt(g(".r-maxsz").value, 10) || 0;
      rule.prefer_words = g(".r-prefer").value.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean);
      rule.exclude_words = g(".r-exclude").value.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean);
      rule.query_template = g(".r-tpl").value.trim() || "{keywords}";
      rule.auto_download = g(".r-auto").checked;
      rule.pick = parseInt(g(".r-pick").value, 10) || 1;
      list.push(rule);
    });
    var out = JSON.parse(JSON.stringify(state.rules || {}));
    out.version = out.version || 1;
    out.rules = list;
    return out;
  }

  function saveRules() {
    var payload = collectRules();
    return api("/rules", { method: "POST", body: { rules: payload } }).then(function (d) {
      if (!d.ok) throw new Error(d.error || "保存失败");
      state.rules = d.rules;
      state.dirty.rules = false;
      updateSaveBar();
      renderRules();
      $("ruleJsonArea").value = JSON.stringify(d.rules, null, 2);
      toast("规则已保存");
      loadStatus();
    });
  }

  /* ---------------- 搜索 ---------------- */
  /* ---------------- 历史字典 ---------------- */
  function fmtSize(n) {
    n = Number(n || 0);
    if (!n) return "—";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)) + " " + u[i];
  }
  function fmtTime(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000), p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function renderLibItems(d) {
    var c = d.counts || {};
    $("libTotal").textContent = c.total || 0;
    $("libTotalSub").textContent = "存在 " + (c.present || 0) + " · 已删除 " + (c.deleted || 0);
    $("libGone").textContent = c.deleted || 0;
    var last = (d.scans || [])[0];
    $("libGoneSub").textContent = last
      ? "上次分析 " + fmtTime(last.ts) + "（新增 " + last.added + " / 消失 " + last.removed + "）"
      : "还没分析过";
    var tb = $("libItemTable").querySelector("tbody");
    var items = d.items || [];
    if (!items.length) {
      tb.innerHTML = '<tr><td colspan="5" class="empty">暂无记录，先点「开始分析」</td></tr>';
      return;
    }
    tb.innerHTML = items.map(function (it) {
      var gone = it.status !== "present";
      return "<tr><td>" + (gone
        ? '<span class="tag err">已删除</span>'
        : '<span class="tag ok">存在</span>') + "</td>" +
        '<td><span class="ellipsis" title="' + esc(it.path) + '">' + esc(it.name) + "</span></td>" +
        "<td>" + fmtSize(it.size) + "</td>" +
        "<td>" + esc(fmtTime(it.first_seen)) + "</td>" +
        "<td>" + esc(gone ? fmtTime(it.deleted_ts) : "—") + "</td></tr>";
    }).join("");
    decorateTables();
  }

  function loadLibrary() {
    var st = val("libFilter");
    return api("/library/items?limit=80" + (st ? "&status=" + encodeURIComponent(st) : ""))
      .then(function (d) { if (d.ok) renderLibItems(d); })
      .catch(function () { });
  }

  function libScan() {
    var btn = $("btnLibScan"), out = $("libOut");
    var roots = String(val("libRoots") || "").split(/\r?\n/)
      .map(function (x) { return x.trim(); }).filter(function (x) { return x.length; });
    if (!roots.length) { toast("先填要分析的目录", true); return; }
    btn.disabled = true;
    out.style.display = "block";
    out.textContent = "分析中…目录大的话要等一会儿";
    api("/library/scan", {
      method: "POST",
      body: {
        roots: roots, max_depth: num("libDepth", 3), max_items: num("libMax", 20000),
        include_files: checked("libFiles")
      }
    }).then(function (d) {
      out.textContent = d.text || JSON.stringify(d, null, 2);
      if (!d.ok) throw new Error(d.error || "分析失败");
      toast("分析完成：新增 " + d.added + "，消失 " + d.removed);
      loadLibrary();
    }).catch(function (e) {
      out.textContent = e.message;
      toast(e.message, true);
    }).then(function () { btn.disabled = false; });
  }

  function libLookup() {
    var q = val("libQuery");
    if (!q) { toast("先输入关键词", true); return; }
    var tb = $("libLookTable").querySelector("tbody");
    tb.innerHTML = '<tr><td colspan="4" class="empty">查询中…</td></tr>';
    api("/library/lookup", { method: "POST", body: { text: q } }).then(function (d) {
      if (!d.ok) throw new Error(d.error || "查询失败");
      var rows = d.matches || [];
      if (!rows.length) {
        tb.innerHTML = '<tr><td colspan="4" class="empty">库里没有这个（' +
          esc(d.verdict || "") + "）</td></tr>";
        toast("库里没有");
        return;
      }
      tb.innerHTML = rows.map(function (m) {
        var gone = m.status !== "present";
        return "<tr><td>" + (gone
          ? '<span class="tag err">曾存在·已删</span>'
          : '<span class="tag ok">已存在</span>') + "</td>" +
          '<td><span class="ellipsis" title="' + esc(m.path) + '">' + esc(m.name) + "</span></td>" +
          '<td class="muted">' + esc(m.path) + "</td>" +
          "<td>" + esc(gone ? fmtTime(m.deleted_ts) : "—") + "</td></tr>";
      }).join("");
      decorateTables();
      toast(d.verdict || "已查询");
    }).catch(function (e) {
      tb.innerHTML = '<tr><td colspan="4" class="empty">' + esc(e.message) + "</td></tr>";
      toast(e.message, true);
    });
  }

  function libDups() {
    var tb = $("libDupTable").querySelector("tbody");
    tb.innerHTML = '<tr><td colspan="5" class="empty">分析中…</td></tr>';
    api("/library/dups", { method: "POST", body: {} }).then(function (d) {
      if (!d.ok) throw new Error(d.error || "分析失败");
      var gs = d.groups || [];
      $("libDupCount").textContent = d.total || 0;
      if (!gs.length) {
        tb.innerHTML = '<tr><td colspan="5" class="empty">没有发现明显重复</td></tr>';
        return;
      }
      tb.innerHTML = gs.map(function (g) {
        return "<tr><td><b>" + esc(g.name) + '</b><br><span class="muted">' +
          g.items.slice(0, 3).map(function (i) { return esc(i.path); }).join("<br>") +
          (g.items.length > 3 ? "<br>…还有 " + (g.items.length - 3) + " 条" : "") +
          "</span></td><td>" + g.count + "</td><td>" + esc(g.size_human) + "</td><td>" +
          esc(g.wasted_human) + "</td><td>" + esc(g.reason) + "</td></tr>";
      }).join("");
      decorateTables();
    }).catch(function (e) {
      tb.innerHTML = '<tr><td colspan="5" class="empty">' + esc(e.message) + "</td></tr>";
      toast(e.message, true);
    });
  }

  /* 解析后搜：先把这段消息按机器人的判定解一遍，把判定结果摆出来，
     再拿判定出的查询真去站点搜。自带磁力的消息不搜，直接把磁力列出来点投递。 */
  function doParseSearch() {
    var q = val("sQuery");
    if (!q) { toast("先粘一段消息", true); return; }
    // 整条磁力没有「消息」可解析，直接走文件清单那条路（也一样能投递）
    if (isMagnet(q)) { doMagnetInspect(q); return; }
    var b1 = $("btnSearch"), b2 = $("btnParseSearch");
    var old = b2.textContent;
    b1.disabled = true; b2.disabled = true; b2.textContent = "解析中…";
    $("searchMeta").textContent = "";
    api("/search/parsed", { method: "POST", body: { text: q, limit: num("sLimit", 10) } })
      .then(function (d) {
        if (!d.ok) throw new Error(d.error || "解析失败");
        renderParse(d, q);
        if (d.search_error) {
          $("searchMeta").innerHTML =
            '<span style="color:#c0392b">站点检索失败：' + esc(d.search_error) + "</span>";
          $("searchResults").innerHTML = "";
        } else if (d.searched) {
          renderResults(d);
        } else {
          $("searchResults").innerHTML = "";
        }
      })
      .catch(function (e) {
        $("parseBox").hidden = true;
        $("searchMeta").innerHTML = '<span style="color:#c0392b">' + esc(e.message) + "</span>";
        $("searchResults").innerHTML = "";
      })
      .then(function () {
        b1.disabled = false; b2.disabled = false; b2.textContent = old;
      });
  }

  /* 下拉里的中文名，规则用的值不在下拉里就原样显示 */
  function optLabel(id, v) {
    var el = $(id);
    if (!el || !v) return "";
    for (var i = 0; i < el.options.length; i++) {
      if (el.options[i].value === v) return el.options[i].text;
    }
    return v;
  }

  function renderParse(d, rawText) {
    var h = [];
    var tone = d.mode === "magnet" ? "hot" : "";
    h.push('<div class="pb-head"><span class="pb-badge ' + tone + '">' +
      esc(d.mode_label || d.mode) + '</span><span class="pb-reason">' +
      esc(d.reason || "") + "</span></div>");

    var rows = [];
    if (d.fc2) {
      rows.push(["FC2 编号", "<code>" + esc(d.fc2.code) + "</code>（原文「" +
        esc(d.fc2.raw) + "」）"]);
    }
    if (d.query) {
      // 标签短一点，手机上才不会被挤成两行；「发给站点」这层意思靠下面那句说明补
      rows.push(["站点查询", "<code>" + esc(d.query) + "</code>"]);
    }
    if (d.rule) {
      // 标签已经写着「命中规则」，值里就不再重复「规则」两个字
      var bits = ["「" + (d.rule.name || d.rule.id) + "」" +
        (d.rule.is_default ? "（默认兜底）" : "")];
      bits.push("分类 " + (d.rule.category ? optLabel("sCategory", d.rule.category) : "站点默认"));
      bits.push("按" + (d.rule.sort ? optLabel("sSort", d.rule.sort) : "站点默认") + "排序");
      if (d.rule.min_seeders !== null && d.rule.min_seeders !== undefined && d.rule.min_seeders !== "") {
        bits.push("做种≥" + d.rule.min_seeders);
      }
      rows.push(["命中规则", esc(bits.join(" · "))]);
    }
    if (d.keywords && d.keywords.length) {
      rows.push(["检索词", esc(d.keywords.join(" "))]);
    }
    if (rows.length) {
      h.push('<div class="pb-rows">' + rows.map(function (r) {
        return '<div class="pb-row"><span class="pb-k">' + r[0] +
          '</span><span class="pb-v">' + r[1] + "</span></div>";
      }).join("") + "</div>");
    }

    var mags = d.magnets || [];
    if (mags.length) {
      h.push('<div class="pb-mags">' + mags.map(function (m, i) {
        return '<div class="pb-mag"><div class="pb-mag-main">' +
          '<span class="pb-mag-title">' + esc(m.title || "未命名磁力") + "</span>" +
          '<span class="pb-mag-hash">' + esc(m.infohash) + "</span></div>" +
          '<button class="btn sm" data-mag-dl="' + i + '">投递</button>' +
          '<button class="btn ghost sm" data-mag-copy="' + i + '">复制</button>' +
          "</div>";
      }).join("") + "</div>");
    }

    // 说清楚「这次用的是什么条件」「为什么没搜」，别让人对着 0 条结果猜
    if (d.note) {
      h.push('<p class="pb-note">' + esc(d.note) + "</p>");
    } else if (d.searched) {
      h.push('<p class="pb-note">检索用的是上面这条查询和规则里的条件，「更多选项」里的分类/排序在这里不生效。</p>');
    }
    if ((d.bad_magnets || []).length) {
      h.push('<p class="pb-warn">忽略：' + d.bad_magnets.map(function (b) {
        return esc(b.length > 40 ? b.slice(0, 40) + "…" : b);
      }).join("、") + "（infohash 不完整，按普通文本处理）</p>");
    }

    var box = $("parseBox");
    box.innerHTML = h.join("");
    box.hidden = false;

    Array.prototype.forEach.call(box.querySelectorAll("[data-mag-dl]"), function (b) {
      b.addEventListener("click", function () {
        var m = mags[parseInt(b.getAttribute("data-mag-dl"), 10)];
        b.disabled = true; b.textContent = "投递中";
        api("/download", { method: "POST", body: { magnet: m.magnet, title: m.title, infohash: m.infohash } })
          .then(function (r) {
            if (r.ok) { toast("已投递到 " + (r.result.backend || "下载器")); b.textContent = "已投递"; }
            else {
              toast("投递失败：" + ((r.result && r.result.message) || r.error), true);
              b.disabled = false; b.textContent = "投递";
            }
          });
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll("[data-mag-copy]"), function (b) {
      b.addEventListener("click", function () {
        copyText(mags[parseInt(b.getAttribute("data-mag-copy"), 10)].magnet);
        toast("磁力链接已复制");
      });
    });
  }

  /* 一键粘贴：https/localhost 下浏览器允许直接读剪贴板；面板常从内网 http 打开，
     那时浏览器不给读，就退回「聚焦输入框 + 说清怎么手动粘」，总比按钮点不动强。 */
  function pasteQuery() {
    var ta = $("sQuery");
    var apply = function (txt) {
      if (!txt || !String(txt).trim()) { toast("剪贴板里没有文字"); ta.focus(); return; }
      var v = ta.value;
      var at = (typeof ta.selectionStart === "number") ? ta.selectionStart : v.length;
      var head = v.slice(0, at), tail = v.slice(at);
      // 光标前是半截词时补个空格，免得粘完黏成「1080pFC2PPV-123」
      var glue = (head && !/\s$/.test(head) && !/^\s/.test(txt)) ? " " : "";
      ta.value = head + glue + txt.trim() + tail;
      var pos = head.length + glue.length + txt.trim().length;
      try { ta.setSelectionRange(pos, pos); } catch (e) { /* ignore */ }
      ta.focus();
      syncQueryTools();
      toast("已粘贴");
    };
    if (navigator.clipboard && navigator.clipboard.readText) {
      navigator.clipboard.readText().then(apply).catch(manualPaste);
    } else {
      manualPaste();
    }
  }

  function manualPaste() {
    var ta = $("sQuery");
    ta.focus();
    try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (e) { /* ignore */ }
    toast("面板走 http 时浏览器不让读剪贴板：长按输入框选「粘贴」即可", true);
  }

  /* 「清空」只在真有字时出现，平时不占地方 */
  /* 「清空」只在真有字时出现；粘的是磁力时，搜索键直接变成「解析磁力」——
     磁力没有关键词可搜，当普通文本送去检索只会搜出一堆无关结果。 */
  function syncQueryTools() {
    var v = $("sQuery").value;
    var mag = isMagnet(v);
    $("btnClearQuery").hidden = !v.trim();
    $("btnSearch").textContent = mag ? "解析磁力" : "直接搜索";
    $("btnSearch").title = mag ? "按当前文件筛选规则看会挑哪几个文件；只问不投" : "";
    $("btnParseSearch").hidden = mag;
    $("sqTipText").textContent = mag ? "回车即解析" : "回车即搜";
  }

  /* 条件收起来的时候也要能一眼看到现在用的哪套，不然「怎么只搜到 5 条」没法自查 */
  function syncSearchAdv() {
    var catEl = $("sCategory"), sortEl = $("sSort");
    var cat = catEl && catEl.selectedIndex >= 0 ? catEl.options[catEl.selectedIndex].text : "全部";
    var sort = sortEl && sortEl.selectedIndex >= 0 ? sortEl.options[sortEl.selectedIndex].text : "做种数";
    $("sAdvSummary").textContent =
      [cat, "按" + sort, "做种≥" + num("sSeeders", 1), num("sLimit", 10) + " 条"].join(" · ");
  }

  function doSearch() {
    var q = val("sQuery");
    if (!q) { toast("先输入关键词", true); return; }
    // 粘的是整条磁力：没有关键词可搜，直接解析文件清单（也顺手能投递）
    if (isMagnet(q)) { doMagnetInspect(q); return; }
    var btn = $("btnSearch");
    btn.disabled = true; btn.textContent = "搜索中…";
    $("searchMeta").textContent = "";
    // 上一次「解析后搜」的判定结果已经跟现在这次检索无关了，先收起来免得看错
    $("parseBox").hidden = true;
    api("/search", {
      method: "POST",
      body: {
        q: q, category: val("sCategory"), sort: val("sSort"),
        min_seeders: num("sSeeders", 1), limit: num("sLimit", 10)
      }
    }).then(function (d) {
      if (!d.ok) throw new Error(d.error || "搜索失败");
      renderResults(d);
    }).catch(function (e) {
      $("searchMeta").innerHTML = '<span style="color:#c0392b">' + esc(e.message) + "</span>";
      $("searchResults").innerHTML = "";
    }).then(function () {
      btn.disabled = false; btn.textContent = "直接搜索";
    });
  }

  function renderResults(d) {
    var drop = d.dropped || {};
    $("searchMeta").innerHTML = "抓取 <b>" + d.total + "</b> 条，通过筛选 <b>" + d.kept +
      "</b> 条" + (d.relaxed ? "（条件过严已放宽）" : "") +
      "　过滤：低做种 " + (drop.seeders || 0) + " / 体积 " + (drop.size || 0) +
      " / 排除词 " + (drop.exclude || 0) + " / 标题 " + (drop.title || 0);
    if (!d.items.length) { $("searchResults").innerHTML = ""; return; }
    $("searchResults").innerHTML = d.items.map(function (it, i) {
      var chips = (it.reasons || []).map(function (r) {
        return '<span class="chip' + (/做种|命中|新发布/.test(r) ? " hot" : "") + '">' + esc(r) + "</span>";
      }).join("");
      var badge = it.trusted ? '<span class="chip trusted">✔ 可信发布（站点绿行）</span>' : "";
      return '<div class="res' + (it.trusted ? " trusted" : "") + '">' +
        '<div class="res-rank">' + (i + 1) + "</div>" +
        '<div class="res-main"><div class="res-title">' + esc(it.title) + "</div>" +
        '<div class="res-meta">' +
        "<span>做种 <b>" + (it.seeders || 0) + "</b></span>" +
        "<span>吸种 <b>" + (it.leechers || 0) + "</b></span>" +
        "<span>完成 <b>" + (it.downloads || 0) + "</b></span>" +
        "<span>体积 <b>" + esc(it.size || "?") + "</b></span>" +
        "<span>" + esc(it.age || "?") + "</span>" +
        "<span>评分 <b>" + (it.score || 0) + "</b></span>" +
        (it.view_url ? '<a href="' + esc(it.view_url) + '" target="_blank" rel="noreferrer">详情页</a>' : "") +
        "</div>" + (badge || chips ? '<div class="chips">' + badge + chips + "</div>" : "") + "</div>" +
        '<div class="res-actions">' +
        '<button class="btn sm" data-dl="' + i + '">投递</button>' +
        '<button class="btn ghost sm" data-copy="' + i + '">磁力</button>' +
        "</div></div>";
    }).join("");

    Array.prototype.forEach.call($("searchResults").querySelectorAll("[data-dl]"), function (b) {
      b.addEventListener("click", function () {
        var it = d.items[parseInt(b.getAttribute("data-dl"), 10)];
        b.disabled = true; b.textContent = "投递中";
        api("/download", { method: "POST", body: { magnet: it.magnet, title: it.title, infohash: it.infohash } })
          .then(function (r) {
            if (r.ok) { toast("已投递到 " + (r.result.backend || "下载器")); b.textContent = "已投递"; }
            else { toast("投递失败：" + ((r.result && r.result.message) || r.error), true); b.disabled = false; b.textContent = "投递"; }
          });
      });
    });
    Array.prototype.forEach.call($("searchResults").querySelectorAll("[data-copy]"), function (b) {
      b.addEventListener("click", function () {
        var it = d.items[parseInt(b.getAttribute("data-copy"), 10)];
        copyText(it.magnet);
        toast("磁力链接已复制");
      });
    });
  }

  /* 复制到剪贴板。面板常是从内网 http 打开的：那种页面上 navigator.clipboard
     根本不存在，所以必须留 execCommand 兜底。
     统一返回 Promise<boolean>：不 reject，调用方拿到 false 自己说失败就行。 */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; },
        function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }
  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-1000px;left:0;opacity:0";
    document.body.appendChild(ta); ta.select();
    try { ta.setSelectionRange(0, ta.value.length); } catch (e) { /* ignore */ }
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  /* ---------------- 探针 ---------------- */
  function runDiag() {
    var btn = $("btnDiag"), out = $("diagOut");
    btn.disabled = true;
    out.hidden = false;
    out.textContent = "诊断中…（最坏十几秒，会分别测直连和代理）";
    api("/diag", { method: "POST", body: {} }).then(function (d) {
      var rep = d.diag || {};
      out.textContent = rep.text || "没有拿到诊断结果";
      var bad = (rep.verdict || []).filter(function (v) { return v[0] === "error"; });
      if (bad.length) toast("诊断发现问题：" + bad[0][1], true);
      else toast("网络诊断完成");
    }).catch(function (e) {
      out.textContent = "诊断失败：" + e.message;
      toast(e.message, true);
    }).then(function () { btn.disabled = false; });
  }

  function probeNyaa() {
    var btn = $("btnNyaaProbe");
    btn.disabled = true;
    api("/probe/nyaa", { method: "POST", body: {} }).then(function (d) {
      if (d.ok) toast("站点连通，" + d.count + " 条结果，" + d.elapsed_ms + "ms");
      else toast("连通失败：" + (d.error || d.hint || "未知"), true);
    }).catch(function (e) { toast(e.message, true); })
      .then(function () { btn.disabled = false; });
  }

  /* 立即重连：重扫本机迅雷网关，打通就写配置并复测（启动时也会自动做一次）。
     概览页的快捷开关和下载页的老按钮共用这一份，btn 传被点的那一个，转圈只锁它自己。 */
  function detectXunlei(btn) {
    btn = btn || $("btnXlReconnect") || $("qXlReconnect");
    var out = $("xProbeOut"), st = $("qXlState");
    if (btn) btn.disabled = true;
    if (out) {
      out.classList.add("show");
      out.textContent = "重连中…（侦测网关端口与应用目录，最多十几秒）";
    }
    if (st) { st.textContent = "重连中…"; st.className = "muted"; }
    api("/xunlei/reconnect", { method: "POST", body: {} }).then(function (d) {
      if (out) out.textContent = d.text || JSON.stringify(d, null, 2);
      if (d.applied) loadConfig();
      var conn = { ok: !!d.ok, port: d.port, text: (d.notes || [])[0] || "" };
      /* 状态行立刻更新，不用等下一轮 /status 轮询 */
      var cs = $("xConnState");
      if (cs) {
        cs.textContent = xlStateText(conn);
        cs.className = "small " + (d.ok ? "muted" : "warn-text");
        cs.title = conn.text;
      }
      syncQuickXunlei(conn);
      var bsub = $("statBackendSub");
      if (bsub) {
        bsub.textContent = xlStateText(conn);
        bsub.className = "stat-sub" + (d.ok ? "" : " warn-text");
        bsub.title = conn.text;
      }
      $("pillBackend").textContent = "后端 迅雷 · " + (d.ok ? "已连接" : "未连接");
      $("pillBackend").title = conn.text;
      toast(d.ok ? "已连上迅雷" : "没连上：" + (conn.text || "未知原因"), !d.ok);
    }).catch(function (e) {
      if (out) out.textContent = e.message;
      if (st) { st.textContent = "重连失败"; st.className = "warn-text"; }
      toast(e.message, true);
    }).then(function () { if (btn) btn.disabled = false; });
  }

  /* 探测结果排版：先给结论，再给原因和建议，最后附原始字段，方便直接复制给人看 */
  function fmtProbe(d) {
    var L = [];
    L.push((d.ok ? "✔ 连接正常" : "✘ 连接失败") + "　" + (d.backend || "-"));
    if (d.url || d.rpc || d.api_base) L.push("地址　" + (d.url || d.rpc || d.api_base));
    /* 线路：代理/WireGuard 环境下最容易怀疑错了对象，先把「走没走代理」写清楚 */
    if (d.route && d.route.why) {
      L.push("线路　" + (d.route.direct ? "直连" : "经代理 " + (d.route.proxy || "-")) +
             "（" + d.route.why + "）");
    }
    if (d.proxy) L.push("代理　" + d.proxy);
    if (d.version) L.push("版本　" + d.version);
    if (d.auth) L.push("鉴权　" + d.auth);
    if (d.token_age) L.push("令牌　" + d.token_age + (d.token_source ? "（" + d.token_source + "）" : ""));
    /* pan_auth 是 JWT，服务端解出了过期时间：过没过期一眼就看出，不用再猜是哪个令牌 */
    if (d.pan_auth_exp) {
      L.push("迅雷令牌 " + d.pan_auth_exp + (d.pan_auth_expired ? "　← 就是它" : ""));
    }
    if (d.culprit) {
      L.push("责任方 " + ({ fnos_token: "fnos-token（飞牛登录态）",
                             pan_auth: "pan_auth（迅雷令牌）",
                             drop_token: "不该带 fnos-token" }[d.culprit] || d.culprit));
    }
    if (d.auth_evidence) L.push("依据　" + d.auth_evidence);
    if (d.target) L.push("设备　" + d.target);
    if (d.folder === "invalid") L.push("目录　id 填的是路径（" + (d.folder_id || "") + "）");
    if (!d.ok && d.error) L.push("原因　" + d.error);
    /* 目录 id 这类「能连通但配置有问题」的情况 ok 仍是 true，提示也要给出来 */
    if (d.hint) L.push((d.ok ? "提示　" : "建议　") + d.hint);
    var extra = {};
    Object.keys(d).forEach(function (k) {
      if (["ok", "backend", "url", "rpc", "proxy", "version", "auth", "target",
           "error", "hint", "folder", "folder_id", "culprit", "auth_evidence",
           "token_age", "token_source", "route", "api_base",
           "pan_auth_exp", "pan_auth_expired"].indexOf(k) < 0) {
        extra[k] = d[k];
      }
    });
    if (Object.keys(extra).length) L.push("其他　" + JSON.stringify(extra));
    return L.join("\n");
  }

  /* 令牌快到期时点胶囊：让程序现在就去本机捞一次，捞到了立刻写配置 */
  function refreshXunleiToken() {
    var tp = $("pillToken");
    var old = tp.textContent;
    tp.disabled = true;
    tp.textContent = "正在重抓令牌…";
    api("/xunlei/refresh-token", { method: "POST", body: {} }).then(function (d) {
      var tk = d.token || {};
      if (d.changed) {
        toast(d.note || "已换用新令牌");
      } else {
        toast((d.note || "本机没有更新的令牌") + "。迅雷套件续期后才有得换", true);
      }
      if (tk.ok && (tk.level === "soon" || tk.level === "expired")) {
        tp.textContent = "迅雷令牌 " + (tk.text || "快到期");
      } else {
        tp.hidden = true;
        tp.textContent = "";
      }
    }).catch(function (e) {
      tp.textContent = old;
      toast(e.message, true);
    }).then(function () { tp.disabled = false; });
  }

  /* 从迅雷任务列表里找回下载目录：那是迅雷自己写过的 id，比自己抄靠谱 */
  function findXunleiFolder() {
    var btn = $("btnXlFolder");
    btn.disabled = true;
    var out = $("xFoldOut");
    out.classList.add("show");
    out.textContent = "正在读迅雷的任务列表…";
    api("/xunlei/folder", { method: "POST", body: { apply: true } }).then(function (d) {
      var fs = d.folders || [];
      if (!fs.length) {
        out.textContent = "没从任务列表里找到目录 id。\n" +
          "可以在迅雷面板 → 文件 → 选中目录，从地址栏 parent_folder_id= 后面复制；" +
          "或者干脆留空，用迅雷默认目录。";
        toast("没找到可用的目录 id", true);
        return;
      }
      var L = [];
      fs.slice(0, 5).forEach(function (f, i) {
        L.push((i + 1) + ". " + (f.parent_folder_path || "(无路径)") +
               "　id=" + f.parent_folder_id + "　出现 " + f.hits + " 次");
      });
      if (d.applied) L.push("已把第 1 条写进设置：" + d.applied + "（记得点保存）");
      out.textContent = L.join("\n");
      if (d.applied) { setVal("xFolder", fs[0].parent_folder_id); toast("已填入下载目录"); }
      else { toast("找到 " + fs.length + " 个候选目录"); }
    }).catch(function (e) {
      out.textContent = e.message;
      toast(e.message, true);
    }).then(function () { btn.disabled = false; });
  }

  /* ── 磁力：粘进搜索框就直接解析文件清单 ───────────────────────────────
     这块原来在设置页「文件筛选」卡片里：填一段磁力就顶出「设置未保存」，想看还得先
     想到去那儿找。现在并进搜索框——粘一条磁力，搜索键自己变成「解析磁力」，点下去
     看到的是「认出几个文件 / 每个文件挑不挑 / 最后发给迅雷什么」。

     用户报「筛选没生效、全下了」时，这里是一眼能看出原因的地方：多半是程序只认出
     1 个条目（迅雷回了个没展开的目录节点），sub_file_index 于是发不出去。 */
  var CAT_LABEL = { video: "视频", subtitle: "字幕", image: "图片",
                    doc: "说明", archive: "压缩包", other: "其他" };

  /* 整段就是一条磁力（不是「消息里夹着磁力」）：去空白后以 magnet: 开头、中间没有空格 */
  function isMagnet(text) {
    var t = String(text || "").trim();
    return /^magnet:\?[^\s]+$/i.test(t) && /[?&]xt=urn:btih:/i.test(t);
  }

  function magnetHash(magnet) {
    var m = /urn:btih:([0-9a-z]+)/i.exec(String(magnet || ""));
    return m ? m[1].toUpperCase() : "";
  }

  function doMagnetInspect(text) {
    var magnet = String(text || "").trim();
    var box = $("parseBox");
    var b1 = $("btnSearch"), b2 = $("btnParseSearch");
    b1.disabled = true; b2.disabled = true; b1.textContent = "解析中…";
    $("searchMeta").textContent = "";
    $("searchResults").innerHTML = "";
    box.hidden = false;
    box.innerHTML = '<p class="pb-note">正在认出这条磁力里有哪些文件…'
      + "迅雷认不出来时会去下种子文件，走代理可能要十几秒。</p>";
    api("/xunlei/parse", { method: "POST", body: { magnet: magnet } }).then(function (d) {
      if (!d.ok) throw new Error(d.error || "解析失败");
      renderMagnet(d, magnet);
    }).catch(function (e) {
      box.hidden = true;
      $("searchMeta").innerHTML = '<span style="color:#c0392b">' + esc(e.message) + "</span>";
    }).then(function () {
      b1.disabled = false; b2.disabled = false;
      syncQueryTools();          // 按钮文案随输入恢复成「解析磁力 / 直接搜索」
    });
  }

  function renderMagnet(d, magnet) {
    var rows = d.files || [];
    var keep = rows.filter(function (r) { return r.keep; }).length;
    var h = [];
    var head, note, ok = rows.length > 1;
    if (!rows.length) {
      head = "一个文件都没认出来";
      note = "认不出清单就只能按全选投递，筛选不起作用。多半是种子文件没下下来"
        + "（走代理常半路断），过一会再点一次；还不行把底下「它试过什么」发我。";
    } else if (rows.length === 1) {
      head = "只认出 1 个文件";
      note = "如果这条磁力里其实有好几个文件，说明只拿到一个目录节点——"
        + "多文件时 sub_file_index 发不出去，结果就是整包全下。";
    } else {
      head = "认出 " + rows.length + " 个文件，按当前规则挑 " + keep + " 个";
      note = "清单来源：" + (d.source === "torrent"
        ? "种子文件（权威清单，序号就是 BT 的文件顺序）"
        : d.source ? "迅雷接口 · " + d.source : "未知");
    }
    h.push('<div class="pb-head"><span class="pb-badge ' + (ok ? "hot" : "warn") + '">'
      + esc(head) + "</span></div>");
    h.push('<p class="pb-note' + (ok ? "" : " warn") + '">' + esc(note) + "</p>");

    if (rows.length) {
      h.push('<div class="ff-rows">');
      rows.forEach(function (r) {
        h.push('<div class="ff-row ' + (r.keep ? "keep" : "drop") + '">'
          + '<span class="mk">' + (r.keep ? "✓" : "✗") + "</span>"
          + '<span class="nm" title="' + esc(r.name) + '">' + esc(r.name) + "</span>"
          + "<span>" + fmtSize(r.size) + "</span>"
          + '<span class="muted">' + (CAT_LABEL[r.cat] || r.cat) + "</span></div>");
      });
      h.push("</div>");
      h.push('<p class="pb-note">发给迅雷的选择：<code>' + esc(d.sub_file_index || "")
        + "</code>" + (/^-1$/.test(d.sub_file_index || "") ? "（全选）" : "") + "</p>");
    }
    if (d.pick_note) h.push('<p class="pb-note">' + esc(d.pick_note) + "</p>");

    var atts = d.attempts || [];
    if (atts.length) {
      h.push('<details class="mag-tries"><summary>它试过什么（' + atts.length
        + " 次）</summary>");
      atts.forEach(function (a) {
        h.push("<div>" + esc(a.variant) + " → "
          + (a.error ? "失败：" + esc(a.error)
            : a.count + " 个文件" + (a.note ? "（" + esc(a.note) + "）" : "")) + "</div>");
      });
      if ((d.raw_keys || []).length) {
        h.push('<div class="mag-raw">响应字段：' + esc(d.raw_keys.join(", ")) + "</div>");
      }
      if (d.raw_shape) {
        h.push('<div class="mag-raw">' + esc(JSON.stringify(d.raw_shape)) + "</div>");
      }
      h.push("</details>");
    }

    h.push('<div class="pb-mags"><div class="pb-mag"><div class="pb-mag-main">'
      + '<span class="pb-mag-title">这条磁力</span>'
      + '<span class="pb-mag-hash">' + esc(magnet) + "</span></div>"
      + '<button class="btn sm" data-mag-dl="0">投递</button>'
      + '<button class="btn ghost sm" data-mag-copy="0">复制</button></div></div>');

    var box = $("parseBox");
    box.innerHTML = h.join("");
    box.hidden = false;
    var dlBtn = box.querySelector("[data-mag-dl]");
    var copyBtn = box.querySelector("[data-mag-copy]");
    copyBtn.addEventListener("click", function () {
      copyText(magnet);
      toast("磁力链接已复制");
    });
    dlBtn.addEventListener("click", function () {
      var b = dlBtn;
      b.disabled = true; b.textContent = "投递中";
      api("/download", { method: "POST",
        body: { magnet: magnet, infohash: magnetHash(magnet) } }).then(function (r) {
        var msg = (r.result && r.result.message) || r.error || "";
        if (r.ok) {
          b.textContent = "已投递";
          toast("已投递到 " + ((r.result && r.result.backend) || "下载器"));
          $("searchMeta").innerHTML = "<span>" + esc(msg) + "</span>";
        } else {
          b.disabled = false; b.textContent = "投递";
          toast("投递失败：" + msg, true);
        }
      }).catch(function (e) {
        b.disabled = false; b.textContent = "投递";
        toast(e.message, true);
      });
    });
  }

  function probeDownloader() {
    var btn = $("btnDlProbe");
    btn.disabled = true;
    var out = $("xProbeOut");
    out.classList.add("show");
    out.textContent = "检测中…";
    api("/probe/downloader", { method: "POST", body: { backend: val("dBackend") } }).then(function (d) {
      out.textContent = fmtProbe(d);
      var warn = d.ok && d.folder === "invalid";
      toast(d.ok ? (warn ? "连接正常，但下载目录 id 填错了" : "连接正常")
                 : "连接异常：" + (d.error || d.hint || ""), !d.ok || warn);
    }).catch(function (e) {
      out.textContent = e.message;
      toast(e.message, true);
    }).then(function () { btn.disabled = false; });
  }

  /* ---------------- 日志 ---------------- */
  function loadLogs() {
    return api("/logs?lines=400").then(function (d) {
      if (d.ok) {
        var box = $("logBox");
        var atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
        box.textContent = (d.lines || []).join("\n") || "（暂无日志）";
        if (atBottom) box.scrollTop = box.scrollHeight;
      }
    }).catch(function () { });
  }

  /* ---------------- 初始化 ---------------- */
  var TITLES = {
    overview: ["概览", "转发给机器人的消息，自动分拣关键词 → 检索 sukebei → 投递迅雷"],
    rules: ["关键词规则", "决定「什么词 → 搜什么 → 怎么筛 → 要不要自动下」"],
    search: ["搜索测试", "不经过 Telegram，直接验证关键词与筛选效果"],
    library: ["历史字典", "记住下过哪些文件；搜索前先告诉你「已经有了」或「什么时候被删了」"],
    download: ["下载与迅雷", "配置迅雷面板连接，可额外挂 aria2 / qBittorrent 兜底"],
    settings: ["设置", "机器人凭证、站点地址与去重策略"],
    logs: ["运行日志", "最近 400 行，含检索与投递细节"]
  };

  /* 底部标签栏上只留概览和搜索两格；规则/下载/字典/设置/日志都从左上角抽屉进，
     所以没有「更多」这一格需要点亮——不在栏里的页面，底部就是两个都灰着 */

  function switchTab(name) {
    if (name === state.tab) { setNav(false); return; }
    var prev = state.tab;
    if (prev && state.dirty[prev]) {
      // 手机上「编辑一半顺手切页」太常见了，切走等于白改，所以拦一下
      setNav(false);          // 抽屉先收起，别让它压在弹窗后面
      askConfirm("「" + ((TITLES[prev] || [prev])[0]) + "」还有改动没保存，切走就丢了。",
        { title: "有未保存的修改", ok: "放弃修改并离开", cancel: "留在这页" })
        .then(function (yes) {
          if (!yes) return;
          state.dirty[prev] = false;
          doSwitch(name);
        });
      return;
    }
    doSwitch(name);
  }

  function doSwitch(name) {
    state.tab = name;
    Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-tab") === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (s) {
      s.classList.toggle("active", s.id === "tab-" + name);
    });
    // 底部标签栏跟着高亮（只认概览/搜索两格）
    Array.prototype.forEach.call(document.querySelectorAll(".tabbar-item[data-tab]"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-tab") === name);
    });
    var t = TITLES[name] || ["", ""];
    $("pageTitle").textContent = t[0];
    $("pageDesc").textContent = t[1];
    /* 说明文字只在概览页留：其余页在手机上是白占一行高度 */
    $("topbar").classList.toggle("show-desc", name === "overview");
    document.title = "磁力管家 · " + t[0];
    setNav(false);
    window.scrollTo(0, 0);
    updateSaveBar();

    clearInterval(state.timer);
    if (name === "logs") {
      loadLogs();
      if (state.logsAuto) state.timer = setInterval(loadLogs, 4000);
    } else if (name === "overview") {
      loadStatus(); loadHistory();
    } else if (name === "rules") {
      loadRules();
    } else if (name === "library") {
      loadLibrary();
    }
  }

  function bind() {
    Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (b) {
      b.addEventListener("click", function () { switchTab(b.getAttribute("data-tab")); });
    });
    $("btnReload").addEventListener("click", function () {
      /* force：手动刷新要能把「刚清除掉的那张表」重新载回来 */
      loadStatus(); loadHistory(true); loadConfig(); loadRules(); toast("已刷新");
    });
    $("btnHistClear").addEventListener("click", clearHistory);
    /* 表格每次重绘，所以勾选/复制都走委托，不给每行绑事件 */
    $("historyTable").addEventListener("change", function (e) {
      if (e.target && e.target.classList && e.target.classList.contains("hpick")) syncPicked();
    });
    $("historyTable").addEventListener("click", function (e) {
      var btn = e.target && e.target.closest ? e.target.closest(".hcopy") : null;
      if (!btn || btn.disabled) return;
      copyText(btn.getAttribute("data-magnet")).then(function (ok) {
        toast(ok ? "磁力已复制" : "复制失败，请在表格里长按标题手动复制", !ok);
      });
    });
    $("btnHistAll").addEventListener("click", function () {
      /* 已经全选了再点就全不选，省一个「清空选择」按钮 */
      var boxes = pickableBoxes();
      var allOn = boxes.length > 0 && pickedMagnets().length === boxes.length;
      setAll(!allOn);
    });
    $("btnHistCopySel").addEventListener("click", copyPicked);
    $("btnCfgSave").addEventListener("click", function () {
      saveConfig().catch(function (e) { toast(e.message, true); });
    });
    $("btnDlSave").addEventListener("click", function () {
      saveConfig().catch(function (e) { toast(e.message, true); });
    });
    $("btnDlProbe").addEventListener("click", probeDownloader);
    /* 代理线路：一个框，一行一条。输入时只更数字和「未保存」标记，
       失焦（change）后再重画结果列表——边打字边刷列表会把「正在填的半个地址」当成一条线 */
    $("btnPcTest").addEventListener("click", testProxyLines);
    $("pcAll").addEventListener("input", function () {
      syncProxyCount();
      dirtySettings();
    });
    $("pcAll").addEventListener("change", function () {
      paintLineResults(readProxyLines(), state.proxyHealth || {});
    });
    $("btnXlFolder").addEventListener("click", findXunleiFolder);
    $("pillToken").addEventListener("click", refreshXunleiToken);
    $("btnLibScan").addEventListener("click", libScan);
    $("btnLibSave").addEventListener("click", function () {
      saveConfig().then(loadLibrary).catch(function (e) { toast(e.message, true); });
    });
    $("btnLibLookup").addEventListener("click", libLookup);
    $("btnLibDup").addEventListener("click", libDups);
    $("btnLibRefresh").addEventListener("click", loadLibrary);
    $("libFilter").addEventListener("change", loadLibrary);
    $("libQuery").addEventListener("keydown", function (e) {
      if (e.key === "Enter") libLookup();
    });
    $("btnXlReconnect").addEventListener("click", function () { detectXunlei($("btnXlReconnect")); });
    $("qXlReconnect").addEventListener("click", function () { detectXunlei($("qXlReconnect")); });
    /* 筛选总开关与各条件联动：改了立刻反映「现在到底会不会生效」 */
    ["xFOn", "xFVideo", "xFSubtitle", "xFImage", "xFDoc", "xFArchive", "xFOther",
      "xFMin", "xFMax", "xFSubExempt"].forEach(function (id) {
      var el = $(id);
      if (!el) return;
      el.addEventListener(el.type === "number" ? "input" : "change", syncFilterBox);
    });
    $("btnNyaaProbe").addEventListener("click", probeNyaa);
    $("btnDiag").addEventListener("click", runDiag);
    $("btnSearch").addEventListener("click", doSearch);
    $("btnParseSearch").addEventListener("click", doParseSearch);
    $("btnPaste").addEventListener("click", pasteQuery);
    $("btnClearQuery").addEventListener("click", function () {
      $("sQuery").value = "";
      $("searchMeta").textContent = "";
      $("parseBox").hidden = true;
      syncQueryTools();
      $("sQuery").focus();
    });
    /* 输入框从单行改成多行：回车直接搜，Shift+回车才是换行 */
    $("sQuery").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); doSearch(); }
    });
    $("sQuery").addEventListener("input", syncQueryTools);
    ["sCategory", "sSort", "sSeeders", "sLimit"].forEach(function (id) {
      $(id).addEventListener("change", syncSearchAdv);
      $(id).addEventListener("input", syncSearchAdv);
    });
    syncQueryTools();
    syncSearchAdv();
    $("btnRuleSave").addEventListener("click", function () {
      saveRules().catch(function (e) { toast(e.message, true); });
    });
    $("btnRuleAdd").addEventListener("click", function () {
      state.rules = collectRules();
      state.rules.rules.push({
        id: "rule_" + Math.random().toString(36).slice(2, 7),
        name: "新规则", enabled: true, match: { any: [] },
        query_template: "{keywords}", category: "0_0", sort: "seeders", limit: 5,
        min_seeders: 1, min_size_mb: 0, max_size_mb: 0,
        prefer_words: [], exclude_words: [], auto_download: true, pick: 1
      });
      renderRules();
    });
    $("btnRuleJson").addEventListener("click", function () {
      var card = $("ruleJsonCard");
      var show = card.style.display === "none";
      card.style.display = show ? "block" : "none";
      if (show) $("ruleJsonArea").value = JSON.stringify(collectRules(), null, 2);
    });
    $("btnRuleJsonFormat").addEventListener("click", function () {
      try { $("ruleJsonArea").value = JSON.stringify(JSON.parse($("ruleJsonArea").value), null, 2); }
      catch (e) { toast("JSON 语法错误：" + e.message, true); }
    });
    $("btnRuleJsonSave").addEventListener("click", function () {
      var data;
      try { data = JSON.parse($("ruleJsonArea").value); }
      catch (e) { toast("JSON 语法错误：" + e.message, true); return; }
      api("/rules", { method: "POST", body: { rules: data } }).then(function (d) {
        if (!d.ok) throw new Error(d.error || "保存失败");
        state.rules = d.rules; renderRules(); toast("规则已保存");
      }).catch(function (e) { toast(e.message, true); });
    });
    $("btnBotRestart").addEventListener("click", function () {
      saveConfig().then(function () { return api("/bot/restart", { method: "POST", body: {} }); })
        .then(function (d) {
          if (d.ok) { toast("机器人已重连"); loadStatus(); }
          else { toast("重连失败：" + (d.error || "未知"), true); }
        }).catch(function (e) { toast(e.message, true); });
    });
    $("btnLogAuto").addEventListener("click", function () {
      state.logsAuto = !state.logsAuto;
      $("btnLogAuto").textContent = "自动刷新：" + (state.logsAuto ? "开" : "关");
      clearInterval(state.timer);
      if (state.logsAuto) state.timer = setInterval(loadLogs, 4000);
    });
    $("btnLogClear").addEventListener("click", function () { $("logBox").textContent = ""; });

    /* ---- 手机导航：汉堡抽屉 + 底部两格标签栏（没有「更多」了，其余页全在抽屉里） ---- */
    $("navToggle").addEventListener("click", function () {
      setNav(!$("app").classList.contains("nav-open"));
    });
    $("navClose").addEventListener("click", function () { setNav(false); });
    $("navShade").addEventListener("click", function () { setNav(false); });
    Array.prototype.forEach.call(document.querySelectorAll(".tabbar-item[data-tab]"), function (b) {
      b.addEventListener("click", function () { switchTab(b.getAttribute("data-tab")); });
    });
    $("sideReload").addEventListener("click", function () {
      loadStatus(); loadConfig(); setNav(false); toast("已刷新");
    });
    $("sidePause").addEventListener("click", function () { togglePause($("sidePause")); });
    // 宽屏下把抽屉状态收掉，免得从手机切换到桌面窗口时页面被锁住滚动
    window.addEventListener("resize", function () { if (!isNarrow()) setNav(false); });

    /* ---- 确认弹窗 ---- */
    $("modalOk").addEventListener("click", function () { closeModal(true); });
    $("modalCancel").addEventListener("click", function () { closeModal(false); });
    $("modal").addEventListener("click", function (e) {
      if (e.target === $("modal")) closeModal(false);   // 点遮罩当取消
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      if (!$("modal").hidden) { closeModal(false); return; }
      setNav(false);
    });

    /* 需要二次确认的按钮：文案写在 HTML 的 data-confirm 里，拦一次、放行一次 */
    document.addEventListener("click", function (e) {
      var el = e.target && e.target.closest ? e.target.closest("[data-confirm]") : null;
      if (!el) return;
      var text = el.getAttribute("data-confirm");
      e.preventDefault();
      e.stopPropagation();
      askConfirm(text, { ok: "继续" }).then(function (yes) {
        if (!yes) return;
        el.removeAttribute("data-confirm");
        try {
          el.click();
        } finally {
          setTimeout(function () { el.setAttribute("data-confirm", text); }, 0);
        }
      });
    }, true);

    /* ---- 概览页快捷开关：改完立刻生效，不用回设置页 ---- */
    $("qAuto").addEventListener("change", function () {
      var el = $("qAuto"), on = el.checked;
      busy(el, function () {
        return patchConfig({ download: { auto_download: on } })
          .then(function () {
            toast(on ? "已开启自动投递" : "已关闭自动投递：只回传磁力，不自动下载");
          })
          .catch(function (e) { el.checked = !on; toast(e.message, true); });
      });
    });
    $("qLibCheck").addEventListener("change", function () {
      var el = $("qLibCheck"), on = el.checked;
      busy(el, function () {
        return patchConfig({ library: { check_search: on } })
          .then(function () { toast(on ? "检索前会先查字典" : "已关闭：检索不再查历史字典"); })
          .catch(function (e) { el.checked = !on; toast(e.message, true); });
      });
    });
    $("qLibSkip").addEventListener("change", function () {
      var el = $("qLibSkip"), on = el.checked;
      busy(el, function () {
        return patchConfig({ library: { skip_download: on } })
          .then(function () { toast(on ? "命中「已下过」时不投递" : "命中也会照常投递（只提示）"); })
          .catch(function (e) { el.checked = !on; toast(e.message, true); });
      });
    });
    $("qBotPause").addEventListener("click", function () { togglePause($("qBotPause")); });

    /* ---- 未保存保护：输入即记脏，切页/关页面前提醒 ---- */
    ["input", "change"].forEach(function (ev) {
      document.querySelector("main").addEventListener(ev, markDirty, true);
    });

    /* 顶栏跟着页面一起滚：不再吸顶，也就不用再监听滚动加分隔线了 */

    $("saveBarSave").addEventListener("click", function () {
      var btn = $("saveBarSave");
      busy(btn, function () {
        var p = state.tab === "rules" ? saveRules() : saveConfig();
        return p.then(updateSaveBar).catch(function (e) { toast(e.message, true); });
      });
    });
    $("saveBarDiscard").addEventListener("click", function () {
      var tab = state.tab;
      state.dirty[tab] = false;
      var p = tab === "rules" ? loadRules() : loadConfig();
      Promise.resolve(p).then(function () {
        updateSaveBar();
        toast("已放弃这页的改动");
      });
    });
    window.addEventListener("beforeunload", function (e) {
      var dirty = Object.keys(state.dirty).some(function (k) { return state.dirty[k]; });
      if (!dirty) return;
      e.preventDefault();
      e.returnValue = "";
      return "";
    });
  }

  bind();
  switchTab("overview");
  loadConfig().then(function () { return loadRules(); });
  setInterval(function () {
    if ($("tab-overview").classList.contains("active")) loadStatus();
  }, 8000);
})();
