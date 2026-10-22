(function () {
  "use strict";

  const TABLE_LABELS = {
    umo_aliases: "会话别名",
    conversations: "对话记录",
    preferences: "偏好/分析记录",
    provider_stats: "调用统计",
    platform_message_history: "平台消息历史",
    platform_sessions: "平台会话（跳过）",
  };
  const STATUS_LABELS = {
    active: "活跃",
    orphan_group: "已退群",
    expired_temp: "过期临时会话",
    missing_friend: "好友列表外",
    whitelisted: "白名单",
  };
  const TYPE_LABELS = { group: "群聊", friend: "好友/私聊" };
  const MAX_LISTED_PREFERENCES = 30;

  const state = {
    tab: "overview",
    overview: null,
    sessionsLoaded: false,
    previewLoaded: false,
    cleanLoaded: false,
    logsLoaded: false,
    settingsLoaded: false,
    cleanJob: null,
  };

  function $(id) {
    return document.getElementById(id);
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function getBridge() {
    return window.AstrBotPluginView || window.AstrBotPluginPage || null;
  }

  async function waitForBridge() {
    for (let i = 0; i < 50; i += 1) {
      const bridge = getBridge();
      if (bridge) return bridge;
      await sleep(100);
    }
    return null;
  }

  async function apiGet(name, params) {
    const bridge = await waitForBridge();
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error("插件页面桥接不可用，请从 AstrBot 插件页面打开");
    }
    return bridge.apiGet(name, params || {});
  }

  async function apiPost(name, body) {
    const bridge = await waitForBridge();
    if (!bridge || typeof bridge.apiPost !== "function") {
      throw new Error("插件页面桥接不可用，请从 AstrBot 插件页面打开");
    }
    return bridge.apiPost(name, body || {});
  }

  function showBanner(message, kind) {
    const banner = $("banner");
    banner.textContent = message;
    banner.className = "banner " + (kind || "error");
    banner.classList.remove("hidden");
  }

  function hideBanner() {
    $("banner").classList.add("hidden");
  }

  function setInlineText(id, text, kind) {
    const node = $(id);
    if (!node) return;
    node.textContent = text || "";
    node.classList.remove("success", "error");
    if (kind) node.classList.add(kind);
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    button.disabled = busy;
    button.textContent = label;
  }

  function fmtTime(value) {
    if (!value) return "--";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString();
  }

  function fmtNum(value) {
    const number = Number(value || 0);
    return Number.isFinite(number) ? number.toLocaleString() : "0";
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function badge(status, label) {
    return el("span", "badge status-" + status, label || STATUS_LABELS[status] || status);
  }

  function metric(label, value, hint) {
    const card = el("div", "metric");
    card.appendChild(el("span", "metric-label", label));
    card.appendChild(el("strong", "metric-value", value));
    if (hint) card.appendChild(el("span", "metric-hint", hint));
    return card;
  }

  function infoRow(label, value) {
    const row = el("div", "info-row");
    row.appendChild(el("span", "info-label", label));
    row.appendChild(el("span", "info-value", value));
    return row;
  }

  function warningList(container, warnings) {
    container.replaceChildren();
    const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
    if (!list.length) {
      container.appendChild(el("p", "muted", "无"));
      return;
    }
    list.forEach(function (text) {
      container.appendChild(el("div", "warning-item", text));
    });
  }

  function countSummary(counts) {
    const parts = [];
    Object.keys(counts || {}).forEach(function (key) {
      const value = Number(counts[key] || 0);
      if (value) parts.push((TABLE_LABELS[key] || key) + " " + value);
    });
    return parts.length ? parts.join(" / ") : "0";
  }

  // ------------------------------------------------------------------
  // 概览
  // ------------------------------------------------------------------

  async function loadOverview() {
    const data = await apiGet("overview");
    state.overview = data;
    state.sessionsLoaded = false;
    state.cleanLoaded = false;
    renderMetrics(data);
    renderPlatform(data);
    renderTables(data);
    warningList($("overview-warnings"), data.warnings);
    const scan = data.scan;
    if (data.needs_scan || !scan) {
      $("subtitle").textContent = "尚未扫描 · 只读";
    } else {
      $("subtitle").textContent = "上次扫描 " + fmtTime(scan.generated_at) + " · 只读";
    }
  }

  function renderMetrics(data) {
    const grid = $("metrics");
    grid.replaceChildren();
    const scan = data.scan || {};
    const platform = scan.platform || {};
    const summary = scan.summary || {};
    grid.appendChild(metric("当前群", platform.groups != null ? fmtNum(platform.groups) : "--", "get_group_list"));
    grid.appendChild(metric("当前好友", platform.friends != null ? fmtNum(platform.friends) : "--", "get_friend_list"));
    grid.appendChild(metric("已退群", summary.orphan_group != null ? fmtNum(summary.orphan_group) : "--", "默认勾选"));
    grid.appendChild(metric("过期临时会话", summary.expired_temp != null ? fmtNum(summary.expired_temp) : "--", "默认不勾选"));
    grid.appendChild(metric("好友列表外", summary.missing_friend != null ? fmtNum(summary.missing_friend) : "--", "默认不勾选"));
    grid.appendChild(metric("候选会话", summary.candidates != null ? fmtNum(summary.candidates) : "--", "可勾选清理"));
  }

  function renderPlatform(data) {
    const box = $("platform-info");
    box.replaceChildren();
    const scan = data.scan || {};
    const platform = scan.platform || {};
    const database = data.database || {};
    box.appendChild(infoRow("平台 ID", scan.platform_id || (data.config || {}).platform_id || "--"));
    box.appendChild(infoRow("群列表", platform.groups != null ? fmtNum(platform.groups) : "--"));
    box.appendChild(infoRow("好友列表", platform.friends != null ? fmtNum(platform.friends) : "--"));
    box.appendChild(infoRow("列表获取时间", fmtTime(platform.captured_at)));
    box.appendChild(infoRow("数据来源", platform.source || "aiocqhttp"));
    box.appendChild(infoRow("主库", database.exists ? "已找到 data_v4.db" : "未找到 data_v4.db"));
    box.appendChild(infoRow("扫描时间", fmtTime(scan.generated_at)));
    $("platform-captured").textContent = platform.captured_at ? "获取于 " + fmtTime(platform.captured_at) : "--";
  }

  function renderTables(data) {
    const body = $("table-body");
    body.replaceChildren();
    const scan = data.scan || {};
    const tables = scan.tables || {};
    const database = data.database || {};
    const counts = database.counts || {};
    let hasRow = false;
    Object.keys(TABLE_LABELS).forEach(function (key) {
      const info = tables[key];
      const total = info ? info.total : counts[key];
      if (info == null && total == null) return;
      hasRow = true;
      const row = document.createElement("tr");
      row.appendChild(el("td", "", TABLE_LABELS[key] || key));
      row.appendChild(el("td", "num", fmtNum(total)));
      row.appendChild(el("td", "num", info && info.orphan != null ? fmtNum(info.orphan) : "--"));
      let note = "";
      if (key === "provider_stats") note = "默认不计入预览";
      if (key === "platform_sessions") note = "当前均为 webchat，跳过";
      if (info && info.skipped) note = "已跳过";
      row.appendChild(el("td", "muted", note));
      body.appendChild(row);
    });
    if (!hasRow) {
      const row = document.createElement("tr");
      const cell = el("td", "muted", "尚未扫描");
      cell.colSpan = 4;
      row.appendChild(cell);
      body.appendChild(row);
    }
  }

  // ------------------------------------------------------------------
  // 会话列表与详情
  // ------------------------------------------------------------------

  async function loadSessions() {
    const status = $("filter-status").value;
    const type = $("filter-type").value;
    const q = $("filter-q").value.trim();
    const data = await apiGet("sessions", { status: status, type: type, q: q, limit: 1000 });
    if (!data.ok) {
      showBanner(data.error || "读取会话列表失败");
      return;
    }
    state.sessionsLoaded = true;
    const body = $("sessions-body");
    body.replaceChildren();
    const items = data.items || [];
    items.forEach(function (item) {
      const row = document.createElement("tr");
      const statusCell = document.createElement("td");
      statusCell.appendChild(badge(item.status, item.status_label));
      row.appendChild(statusCell);
      row.appendChild(el("td", "", TYPE_LABELS[item.kind] || item.kind));
      const nameCell = document.createElement("td");
      nameCell.appendChild(document.createTextNode(item.name || "--"));
      if (Number((item.counts || {}).conversations || 0) > 0) {
        const pill = el("span", "pill soft", "有对话记录");
        pill.style.marginLeft = "6px";
        nameCell.appendChild(pill);
      }
      row.appendChild(nameCell);
      row.appendChild(el("td", "mono", item.session_id || "--"));
      row.appendChild(el("td", "", fmtTime(item.last_active)));
      const rowsCell = el("td", "num", fmtNum(item.total_rows));
      rowsCell.title = countSummary(item.counts);
      row.appendChild(rowsCell);
      row.appendChild(el("td", "notes", (item.notes || []).join("；")));
      const actionCell = document.createElement("td");
      const detailButton = el("button", "ghost small", "详情");
      detailButton.type = "button";
      detailButton.addEventListener("click", function () { openDetail(item.umo); });
      actionCell.appendChild(detailButton);
      row.appendChild(actionCell);
      body.appendChild(row);
    });
    $("sessions-empty").classList.toggle("hidden", items.length > 0);
    $("sessions-summary").textContent = "共 " + fmtNum(data.total) + " 个会话";
    return data;
  }

  async function openDetail(umo) {
    const modal = $("detail-modal");
    modal.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    $("detail-title").textContent = "会话详情";
    const body = $("detail-body");
    body.replaceChildren(el("p", "muted", "正在读取详情…"));
    try {
      const data = await apiGet("session_detail", { umo: umo, limit: 20 });
      if (!data.ok) {
        body.replaceChildren(el("p", "error-text", data.error || "读取详情失败"));
        return;
      }
      renderDetail(data.detail);
    } catch (error) {
      body.replaceChildren(el("p", "error-text", error && error.message ? error.message : String(error)));
    }
  }

  function closeDetail() {
    $("detail-modal").classList.add("hidden");
    document.body.style.overflow = "";
  }

  function sectionTitle(text) {
    const head = el("div", "detail-section");
    head.appendChild(el("h3", "", text));
    return head;
  }

  async function loadConversation(conversationId, umo, container, button) {
    if (!container.classList.contains("hidden")) {
      container.classList.add("hidden");
      button.textContent = "查看对话记录";
      return;
    }
    button.disabled = true;
    button.textContent = "加载中…";
    container.classList.remove("hidden");
    container.replaceChildren(el("p", "muted", "正在读取核心库对话记录…"));
    try {
      const data = await apiGet("conversation_detail", {
        conversation_id: conversationId,
        umo: umo,
        limit: 200,
      });
      if (!data.ok) {
        container.replaceChildren(el("p", "error-text", data.error || "读取对话记录失败"));
        return;
      }
      renderConversationMessages(data.detail, container);
      button.textContent = "收起对话记录";
    } catch (error) {
      container.replaceChildren(el(
        "p",
        "error-text",
        error && error.message ? error.message : String(error)
      ));
    } finally {
      button.disabled = false;
    }
  }

  function renderConversationMessages(detail, container) {
    container.replaceChildren();
    const info = "共 " + fmtNum(detail.message_count) + " 条消息";
    const truncated = detail.truncated
      ? "，当前显示最近 " + fmtNum(detail.shown_messages) + " 条"
      : "";
    container.appendChild(el(
      "p",
      "detail-note",
      info + truncated + " · 更新于 " + fmtTime(detail.updated_at)
    ));
    if (!detail.messages || !detail.messages.length) {
      container.appendChild(el("p", "empty", "这条对话记录里没有可显示的消息。"));
      return;
    }
    const list = el("div", "message-list");
    detail.messages.forEach(function (message, index) {
      const row = el("div", "message-item chat-" + (message.role || "unknown"));
      const meta = el("div", "message-meta");
      meta.appendChild(el("strong", "", message.role_label || message.role || "未知"));
      meta.appendChild(el("span", "", "#" + (index + 1)));
      if (message.images) meta.appendChild(el("span", "", "附件 " + message.images + " 个"));
      row.appendChild(meta);
      (message.texts || []).forEach(function (text) {
        row.appendChild(el("div", "message-content", text));
      });
      if (!(message.texts || []).length && (message.images || 0)) {
        row.appendChild(el("div", "message-content muted", "[图片/附件 " + message.images + " 个]"));
      }
      if ((message.thinks || []).length) {
        const details = document.createElement("details");
        details.appendChild(el("summary", "", "思考过程（" + message.thinks.length + "）"));
        message.thinks.forEach(function (text) {
          details.appendChild(el("div", "message-content muted", text));
        });
        row.appendChild(details);
      }
      if ((message.tools || []).length) {
        const details = document.createElement("details");
        details.appendChild(el("summary", "", "工具调用（" + message.tools.length + "）"));
        message.tools.forEach(function (text) {
          details.appendChild(el("pre", "detail-pre", text));
        });
        row.appendChild(details);
      }
      list.appendChild(row);
    });
    container.appendChild(list);
    if ((detail.warnings || []).length) {
      const warnings = el("div", "warnings");
      warningList(warnings, detail.warnings);
      container.appendChild(warnings);
    }
  }

  function renderDetail(detail) {
    const body = $("detail-body");
    body.replaceChildren();
    $("detail-title").textContent =
      (detail.name || detail.session_id || "会话详情") + " · " + (TYPE_LABELS[detail.kind] || "会话");

    const grid = el("div", "detail-grid");

    const basic = el("div", "card inset");
    basic.appendChild(el("h3", "", "基本信息"));
    const statusRow = el("div", "info-row");
    statusRow.appendChild(el("span", "info-label", "状态"));
    const statusValue = el("span", "info-value");
    statusValue.appendChild(badge(detail.status, detail.status_label));
    statusRow.appendChild(statusValue);
    basic.appendChild(statusRow);
    basic.appendChild(infoRow("类型", TYPE_LABELS[detail.kind] || detail.kind || "--"));
    basic.appendChild(infoRow(detail.kind === "group" ? "群号" : "QQ号", detail.session_id || "--"));
    basic.appendChild(infoRow(detail.kind === "group" ? "群名" : "QQ昵称", detail.name || "--"));
    basic.appendChild(infoRow("UMO", detail.umo || "--"));
    basic.appendChild(infoRow("最后活跃", fmtTime(detail.last_active)));
    basic.appendChild(infoRow(
      "是否可清理",
      detail.selectable
        ? (detail.default_selected ? "可清理（默认勾选）" : "可清理（默认不勾选）")
        : "不可清理"
    ));
    if ((detail.alias_names || []).length) {
      const aliasRow = el("div", "info-row");
      aliasRow.appendChild(el("span", "info-label", "别名"));
      const list = el("div", "alias-list");
      detail.alias_names.forEach(function (name) {
        list.appendChild(el("span", "alias-chip", name));
      });
      aliasRow.appendChild(list);
      basic.appendChild(aliasRow);
    }
    grid.appendChild(basic);

    const countsCard = el("div", "card inset");
    countsCard.appendChild(el("h3", "", "残留数据分布"));
    const tableWrap = el("div", "table-wrap");
    const table = el("table", "table");
    const thead = el("thead");
    const headRow = el("tr");
    ["数据表", "条数", "说明"].forEach(function (text) {
      headRow.appendChild(el("th", "", text));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el("tbody");
    Object.keys(TABLE_LABELS).forEach(function (key) {
      if (key === "platform_sessions") return;
      const value = Number((detail.counts || {})[key] || 0);
      const row = document.createElement("tr");
      row.appendChild(el("td", "", TABLE_LABELS[key]));
      row.appendChild(el("td", "num", fmtNum(value)));
      let note = value ? "" : "无";
      if (key === "provider_stats") note = value ? "默认不清理" : "无";
      row.appendChild(el("td", "muted", note));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    countsCard.appendChild(tableWrap);
    if ((detail.provider_stats || {}).count) {
      countsCard.appendChild(el(
        "p",
        "detail-note",
        "调用统计时间范围：" + fmtTime(detail.provider_stats.first_at) + " ～ " + fmtTime(detail.provider_stats.last_at)
      ));
    }
    grid.appendChild(countsCard);
    body.appendChild(grid);

    if ((detail.notes || []).length) {
      const notes = el("div", "detail-section");
      notes.appendChild(el("h3", "", "扫描说明"));
      const warnings = el("div", "warnings");
      warningList(warnings, detail.notes);
      notes.appendChild(warnings);
      body.appendChild(notes);
    }

    const conversations = detail.conversations || [];
    const convSection = sectionTitle("核心库对话记录（" + conversations.length + " 条）");
    convSection.appendChild(el(
      "p",
      "detail-note",
      "这些记录来自 AstrBot 核心库 conversations 表，和 Dashboard「数据与日志 → 对话」是同一份数据。" +
      "只有真正和机器人进行过 LLM 对话的会话才会有记录；没有记录不代表数据丢了。"
    ));
    if (!conversations.length) {
      convSection.appendChild(el(
        "p",
        "empty",
        "该会话在核心库 conversations 表里没有对话记录。QQ 群/好友的普通聊天正文不在核心框架里；" +
        "本插件不读取任何插件数据，所以这里只能显示核心库实际保存的内容。"
      ));
    } else {
      conversations.forEach(function (item) {
        const card = el("div", "card inset");
        const head = el("div", "card-head");
        const titleWrap = el("div");
        titleWrap.appendChild(el("strong", "", item.title || "无标题对话"));
        titleWrap.appendChild(el("div", "muted",
          "更新于 " + fmtTime(item.updated_at) + " · 内容 " + fmtNum(item.content_length) + " 字"));
        head.appendChild(titleWrap);
        const viewButton = el("button", "ghost small", "查看对话记录");
        viewButton.type = "button";
        head.appendChild(viewButton);
        card.appendChild(head);
        const viewer = el("div", "conversation-viewer hidden");
        viewButton.addEventListener("click", function () {
          loadConversation(item.conversation_id, detail.umo, viewer, viewButton);
        });
        card.appendChild(viewer);
        convSection.appendChild(card);
      });
    }
    body.appendChild(convSection);

    const preferences = detail.preferences || [];
    const prefSection = sectionTitle("偏好/分析记录（" + preferences.length + " 条）");
    if (!preferences.length) {
      prefSection.appendChild(el("p", "empty", "没有命中该会话的偏好记录。"));
    } else {
      const list = el("div", "message-list");
      preferences.slice(0, MAX_LISTED_PREFERENCES).forEach(function (item) {
        const row = el("div", "message-item");
        const meta = el("div", "message-meta");
        meta.appendChild(el("span", "mono", item.key || "(无 key)"));
        meta.appendChild(el("span", "", item.scope_id || ""));
        meta.appendChild(el("span", "", fmtTime(item.last_active)));
        row.appendChild(meta);
        row.appendChild(el("div", "message-content", item.value_preview || ""));
        list.appendChild(row);
      });
      prefSection.appendChild(list);
      if (preferences.length > MAX_LISTED_PREFERENCES) {
        prefSection.appendChild(el(
          "p",
          "detail-note",
          "只显示前 " + MAX_LISTED_PREFERENCES + " 条，另有 " + (preferences.length - MAX_LISTED_PREFERENCES) + " 条未显示。"
        ));
      }
    }
    body.appendChild(prefSection);

    const messages = detail.core_messages || [];
    const msgSection = sectionTitle("核心库平台消息历史（" + messages.length + " 条）");
    msgSection.appendChild(el(
      "p",
      "detail-note",
      "只读取 AstrBot 核心库 platform_message_history。QQ 群/好友的聊天正文不由核心框架保存，插件数据一律不读取。"
    ));
    if (!messages.length) {
      msgSection.appendChild(el(
        "p",
        "empty",
        "核心库里没有该会话的平台消息历史。当前核心库的 47 条消息全部来自 WebChat；QQ 会话正文不在核心库。"
      ));
    } else {
      const list = el("div", "message-list");
      messages.forEach(function (item) {
        const row = el("div", "message-item");
        const meta = el("div", "message-meta");
        meta.appendChild(el("span", "", item.sender_name || item.sender_id || "未知发送者"));
        meta.appendChild(el("span", "", fmtTime(item.at)));
        row.appendChild(meta);
        row.appendChild(el("div", "message-content", item.content || "(空消息)"));
        list.appendChild(row);
      });
      msgSection.appendChild(list);
    }
    body.appendChild(msgSection);

    if ((detail.warnings || []).length) {
      const warningSection = sectionTitle("读取提示");
      const warnings = el("div", "warnings");
      warningList(warnings, detail.warnings);
      warningSection.appendChild(warnings);
      body.appendChild(warningSection);
    }
  }

  // ------------------------------------------------------------------
  // 清理预览
  // ------------------------------------------------------------------

  async function loadPreview() {
    const include = $("include-provider-stats").checked;
    const data = await apiPost("preview", {
      include_provider_stats: include,
    });
    if (!data.ok) {
      showBanner(data.error || "生成预览失败");
      return;
    }
    const preview = data.preview;
    state.previewLoaded = true;

    const grid = $("preview-metrics");
    grid.replaceChildren();
    grid.appendChild(metric("候选会话", fmtNum(preview.summary.targets), "当前列表"));
    grid.appendChild(metric("默认勾选", fmtNum(preview.summary.default_targets), "已退群"));
    grid.appendChild(metric("将处理行数", fmtNum(preview.summary.total_rows), include ? "含调用统计" : "不含调用统计"));
    grid.appendChild(metric("清理模式", "备份后删除", "核心库"));

    const groups = $("preview-groups");
    groups.replaceChildren();
    (preview.groups || []).forEach(function (group) {
      const card = el("div", "card inset");
      const head = el("div", "card-head");
      head.appendChild(el("h3", "", group.label));
      head.appendChild(el("span", "muted", group.count + " 个会话"));
      card.appendChild(head);
      const wrap = el("div", "table-wrap");
      const table = el("table", "table");
      const thead = el("thead");
      const headRow = el("tr");
      ["名称", "群号/QQ", "最后活跃", "候选行数", "说明"].forEach(function (text) {
        headRow.appendChild(el("th", "", text));
      });
      thead.appendChild(headRow);
      table.appendChild(thead);
      const tbody = el("tbody");
      (group.items || []).forEach(function (item) {
        const row = el("tr");
        row.appendChild(el("td", "", item.name || "--"));
        row.appendChild(el("td", "mono", item.session_id || "--"));
        row.appendChild(el("td", "", fmtTime(item.last_active)));
        const countCell = el("td", "num", fmtNum(item.total_rows));
        countCell.title = countSummary(item.counts);
        row.appendChild(countCell);
        row.appendChild(el("td", "notes", (item.notes || []).join("；")));
        tbody.appendChild(row);
      });
      wrap.appendChild(table);
      card.appendChild(wrap);
      groups.appendChild(card);
    });
    if (!(preview.groups || []).length) {
      groups.appendChild(el("p", "empty", "没有可清理的候选。"));
    }

    const actionBody = $("preview-actions-body");
    actionBody.replaceChildren();
    (preview.actions || []).forEach(function (action) {
      const row = document.createElement("tr");
      row.appendChild(el("td", "", action.label || action.table));
      row.appendChild(el("td", "notes", action.condition || ""));
      row.appendChild(el("td", "num", fmtNum(action.count)));
      row.appendChild(el("td", "muted", action.optional ? "可选" : ""));
      actionBody.appendChild(row);
    });
    if (!(preview.actions || []).length) {
      const row = document.createElement("tr");
      const cell = el("td", "muted", "没有需要处理的数据");
      cell.colSpan = 4;
      row.appendChild(cell);
      actionBody.appendChild(row);
    }

    warningList($("preview-warnings"), preview.warnings);
    return preview;
  }

  // ------------------------------------------------------------------
  // 执行清理与回滚
  // ------------------------------------------------------------------

  async function loadCleanTab() {
    const data = await apiGet("sessions", { limit: 2000 });
    if (!data.ok) {
      showBanner(data.error || "读取候选失败");
      return;
    }
    state.cleanLoaded = true;
    const box = $("clean-targets");
    box.replaceChildren();
    const providerSelected = $("clean-include-provider-stats").checked;
    function onlyProvider(item) {
      const provider = Number((item.counts || {}).provider_stats || 0);
      return provider > 0 && Number(item.total_rows || 0) - provider <= 0;
    }
    const candidates = (data.items || [])
      .filter(function (item) { return item.selectable; })
      .sort(function (a, b) {
        return Number(onlyProvider(a)) - Number(onlyProvider(b));
      });
    if (!candidates.length) {
      box.appendChild(el("p", "empty", "当前没有可清理候选。"));
    }
    candidates.forEach(function (item) {
      const only = onlyProvider(item);
      const row = el("label", "candidate-item" + (only ? " candidate-only-provider" : ""));
      row.dataset.onlyProvider = only ? "1" : "0";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = item.umo;
      checkbox.checked = only
        ? (providerSelected && item.status === "orphan_group")
        : Boolean(item.default_selected);
      const main = el("div", "candidate-main");
      const title = el("div", "candidate-title");
      title.appendChild(badge(item.status, item.status_label));
      title.appendChild(el("strong", "", item.name || "--"));
      title.appendChild(el("span", "mono", item.session_id || ""));
      if (only) title.appendChild(el("span", "pill soft", "仅剩调用统计"));
      main.appendChild(title);
      const providerRows = Number((item.counts || {}).provider_stats || 0);
      main.appendChild(el("div", "candidate-meta",
        (TYPE_LABELS[item.kind] || item.kind) + " · 候选行数 " + fmtNum(item.total_rows) +
        (only ? " · 其中 provider_stats " + fmtNum(providerRows) + " 条，默认不清理" : "") +
        " · " + (item.notes || []).join("；")));
      row.appendChild(checkbox);
      row.appendChild(main);
      box.appendChild(row);
    });
    applyCleanVisibility();
    await loadBackups();
    return data;
  }

  function applyCleanVisibility() {
    const showOnly = $("show-provider-only").checked || $("clean-include-provider-stats").checked;
    let hidden = 0;
    document.querySelectorAll("#clean-targets .candidate-item").forEach(function (row) {
      const only = row.dataset.onlyProvider === "1";
      row.classList.toggle("hidden", only && !showOnly);
      if (only && !showOnly) {
        const input = row.querySelector("input");
        if (input) input.checked = false;
        hidden += 1;
      }
    });
    const note = $("provider-only-note");
    if (!note) return;
    if (!hidden) {
      note.textContent = "";
      note.classList.add("hidden");
      return;
    }
    note.classList.remove("hidden");
    note.textContent =
      "另有 " + hidden + " 个会话只剩余 provider_stats 调用统计，默认不清理，已隐藏；" +
      "需要查看时勾选“显示仅剩调用统计的条目”。";
  }

  function syncProviderOnlySelection() {
    const enabled = $("clean-include-provider-stats").checked;
    document.querySelectorAll("#clean-targets .candidate-item").forEach(function (row) {
      if (row.dataset.onlyProvider !== "1") return;
      const input = row.querySelector("input");
      if (input) input.checked = enabled;
    });
    applyCleanVisibility();
  }

  async function loadBackups() {
    const data = await apiGet("backups", { limit: 20 });
    if (!data.ok) {
      showBanner(data.error || "读取备份失败");
      return;
    }
    const box = $("backups-list");
    box.replaceChildren();
    const items = data.items || [];
    if (!items.length) {
      box.appendChild(el("p", "empty", "还没有备份。"));
      return;
    }
    items.forEach(function (item) {
      const row = el("div", "backup-item");
      row.appendChild(el("span", "backup-id", item.backup_id || "--"));
      row.appendChild(el("span", "backup-status " + (item.status || ""), item.status || "unknown"));
      const counts = item.counts
        ? Object.keys(item.counts).map(function (key) { return key + " " + item.counts[key]; }).join(" / ")
        : "";
      row.appendChild(el("span", "backup-counts", fmtTime(item.created_at) + (counts ? " · " + counts : "")));
      const button = el("button", "ghost", "回滚");
      button.type = "button";
      button.addEventListener("click", function () { rollbackBackup(item.backup_id, button); });
      row.appendChild(button);
      box.appendChild(row);
    });
    return items;
  }

  function showProgress(percent, stage, message, startedAt) {
    const box = $("clean-progress");
    box.classList.remove("hidden");
    const safe = Math.max(0, Math.min(100, Number(percent) || 0));
    $("clean-progress-stage").textContent = stage || "处理中";
    $("clean-progress-percent").textContent = Math.round(safe) + "%";
    $("clean-progress-bar").style.width = safe + "%";
    const elapsed = startedAt ? Math.round((Date.now() - startedAt) / 1000) : 0;
    $("clean-progress-message").textContent =
      (message || "") + (startedAt ? " · 已用 " + elapsed + " 秒" : "");
  }

  function hideProgress() {
    $("clean-progress").classList.add("hidden");
  }

  async function executeClean() {
    setInlineText("clean-error", "");
    setInlineText("clean-status", "");
    const targets = Array.from(document.querySelectorAll("#clean-targets input:checked"))
      .map(function (input) { return input.value; });
    if (!targets.length) {
      setInlineText("clean-error", "请至少勾选一个会话", "error");
      $("clean-targets").scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const confirm = $("clean-confirm").value.trim();
    if (confirm !== "确认删除") {
      setInlineText("clean-error", "请输入“确认删除”四个字", "error");
      $("clean-confirm").focus();
      return;
    }
    if (state.cleanJob) {
      setInlineText("clean-error", "已有清理任务正在执行，请等待完成", "error");
      return;
    }
    const button = $("clean-btn");
    const startedAt = Date.now();
    setBusy(button, true, "清理中…");
    hideBanner();
    showProgress(0, "正在创建清理任务", "请稍候", startedAt);
    try {
      const start = await apiPost("clean_start", {
        targets: targets,
        include_provider_stats: $("clean-include-provider-stats").checked,
        confirm: "确认删除",
      });
      if (!start.ok) {
        setInlineText("clean-error", start.error || "清理启动失败", "error");
        hideProgress();
        return;
      }
      state.cleanJob = start.job_id;
      let polls = 0;
      while (true) {
        await sleep(700);
        polls += 1;
        if (polls > 900) {
          setInlineText(
            "clean-error",
            "任务已运行超过 10 分钟，可能仍在后台执行；请稍后刷新查看备份和扫描结果",
            "error"
          );
          hideProgress();
          break;
        }
        const data = await apiGet("clean_status", { job_id: start.job_id });
        if (!data.ok) {
          setInlineText("clean-error", data.error || "读取清理进度失败", "error");
          hideProgress();
          return;
        }
        const job = data.job;
        showProgress(job.percent, job.stage, job.message, startedAt);
        if (job.status === "success") {
          const payload = job.result || {};
          $("clean-confirm").value = "";
          if (payload.result) renderCleanResult(payload.result, payload.warning);
          setInlineText(
            "clean-status",
            "清理完成，备份 " + (payload.result ? payload.result.backup_id : "--"),
            "success"
          );
          setInlineText("clean-error", "");
          showProgress(100, "清理完成", "列表已刷新", startedAt);
          await loadOverview();
          await loadCleanTab();
          break;
        }
        if (job.status === "error") {
          const message = job.error || (job.result && job.result.error) || "清理失败";
          setInlineText("clean-error", message, "error");
          setInlineText("clean-status", "");
          hideProgress();
          break;
        }
      }
    } catch (error) {
      setInlineText(
        "clean-error",
        (error && error.message ? error.message : String(error)) +
          "（任务可能仍在后台运行，可稍后刷新查看备份）",
        "error"
      );
      hideProgress();
    } finally {
      state.cleanJob = null;
      setBusy(button, false, "执行清理");
    }
  }

  function renderCleanResult(result, warning) {
    const box = $("clean-result");
    box.replaceChildren();
    if (!result) return;
    const counts = result.counts || {};
    box.appendChild(el("div", "warning-item",
      "备份 ID：" + (result.backup_id || "--") + "；删除 " + fmtNum(result.deleted) + " 行：" +
      Object.keys(counts).map(function (key) { return key + " " + counts[key]; }).join(" / ")));
    (result.warnings || []).forEach(function (text) {
      box.appendChild(el("div", "warning-item", text));
    });
    if (warning) box.appendChild(el("div", "warning-item", warning));
  }

  async function rollbackBackup(backupId, button) {
    setInlineText("rollback-status", "");
    const confirm = $("rollback-confirm").value.trim();
    if (confirm !== "RESTORE") {
      setInlineText("rollback-status", "请输入 RESTORE 确认回滚", "error");
      $("rollback-confirm").focus();
      return;
    }
    setBusy(button, true, "回滚中…");
    try {
      const data = await apiPost("rollback", { backup_id: backupId, confirm: "RESTORE" });
      if (!data.ok) {
        setInlineText("rollback-status", data.error || "回滚失败", "error");
        return;
      }
      $("rollback-confirm").value = "";
      const restored = data.result && data.result.restored ? data.result.restored : {};
      const summary = Object.keys(restored).map(function (key) { return key + " " + restored[key]; }).join(" / ");
      setInlineText("rollback-status", "回滚完成：" + (summary || "无行需要恢复"), "success");
      await loadOverview();
      await loadCleanTab();
    } catch (error) {
      setInlineText("rollback-status", error && error.message ? error.message : String(error), "error");
    } finally {
      setBusy(button, false, "回滚");
    }
  }

  // ------------------------------------------------------------------
  // 日志与设置
  // ------------------------------------------------------------------

  async function loadLogs() {
    const data = await apiGet("logs", { limit: 50 });
    if (!data.ok) {
      showBanner(data.error || "读取日志失败");
      return;
    }
    state.logsLoaded = true;
    const box = $("logs-list");
    box.replaceChildren();
    const items = data.items || [];
    if (!items.length) {
      box.appendChild(el("p", "empty", "还没有扫描日志。"));
      return;
    }
    items.forEach(function (item) {
      const row = el("div", "log-item");
      row.appendChild(el("span", "log-time", fmtTime(item.ts)));
      const result = item.ok ? "成功" : "失败";
      row.appendChild(el("span", "log-result " + (item.ok ? "ok" : "bad"), result));
      const detail = [];
      if (item.event) detail.push(item.event);
      if (item.platform_id) detail.push("平台 " + item.platform_id);
      if (item.groups != null) detail.push("群 " + item.groups);
      if (item.friends != null) detail.push("好友 " + item.friends);
      if (item.targets != null) detail.push("会话 " + item.targets);
      if (item.candidates != null) detail.push("候选 " + item.candidates);
      if (item.deleted != null) detail.push("删除 " + item.deleted + " 行");
      if (item.error) detail.push(item.error);
      row.appendChild(el("span", "log-detail", detail.join(" · ")));
      box.appendChild(row);
    });
  }

  function settingsField(label, input, hint, options) {
    const opts = options || {};
    if (opts.switch) {
      const row = el("label", "settings-switch" + (opts.full ? " full" : ""));
      const copy = el("div", "switch-copy");
      copy.appendChild(el("strong", "", label));
      if (hint) copy.appendChild(el("small", "", hint));
      row.appendChild(copy);
      row.appendChild(input);
      return row;
    }
    const field = el("label", "settings-field" + (opts.full ? " full" : ""));
    field.appendChild(el("span", "settings-label", label));
    field.appendChild(input);
    if (hint) field.appendChild(el("small", "field-hint", hint));
    return field;
  }

  function settingsSection(form, title, description) {
    const section = el("section", "card settings-section");
    const head = el("div", "settings-section-head");
    const copy = el("div");
    copy.appendChild(el("h3", "", title));
    if (description) copy.appendChild(el("p", "", description));
    head.appendChild(copy);
    section.appendChild(head);
    const grid = el("div", "settings-grid");
    section.appendChild(grid);
    form.appendChild(section);
    return grid;
  }

  function textInput(name, value, placeholder) {
    const input = document.createElement("input");
    input.type = "text";
    input.name = name;
    input.value = value == null ? "" : String(value);
    if (placeholder) input.placeholder = placeholder;
    return input;
  }

  function numberInput(name, value, min, max, step) {
    const input = document.createElement("input");
    input.type = "number";
    input.name = name;
    input.value = value == null ? "" : String(value);
    input.min = String(min);
    input.max = String(max);
    input.step = String(step || 1);
    return input;
  }

  function checkInput(name, checked) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = name;
    input.checked = Boolean(checked);
    return input;
  }

  function textAreaInput(name, values, placeholder) {
    const input = document.createElement("textarea");
    input.name = name;
    input.value = Array.isArray(values) ? values.join("\n") : (values || "");
    if (placeholder) input.placeholder = placeholder;
    return input;
  }

  async function loadSettings() {
    const data = await apiGet("settings");
    if (!data.ok) {
      showBanner(data.error || "读取配置失败");
      return;
    }
    state.settingsLoaded = true;
    const form = $("settings-form");
    form.replaceChildren();
    const config = data.config || {};

    const scanGrid = settingsSection(
      form,
      "扫描设置",
      "控制平台列表读取、临时会话判定和备份策略。"
    );
    scanGrid.appendChild(settingsField(
      "平台实例 ID",
      textInput("platform_id", config.platform_id || "default", "default"),
      "aiocqhttp 默认 default；修改后下次扫描生效。"
    ));
    scanGrid.appendChild(settingsField(
      "临时会话阈值（天）",
      numberInput("temp_session_days", config.temp_session_days, 1, 365, 1),
      "好友列表外、超过该天数未活跃的会话视为过期临时会话。"
    ));
    scanGrid.appendChild(settingsField(
      "平台列表超时（秒）",
      numberInput("scan_timeout_seconds", config.scan_timeout_seconds, 3, 60, 1),
      "get_group_list / get_friend_list 的超时；超时即停止扫描。"
    ));
    scanGrid.appendChild(settingsField(
      "备份保留份数",
      numberInput("backup_retain", config.backup_retain, 1, 30, 1),
      "每次清理前生成备份，超过份数自动删除最旧的备份。"
    ));

    const scopeGrid = settingsSection(
      form,
      "清理范围",
      "决定哪些核心库数据会被清理；插件数据一律不读取。"
    );
    scopeGrid.appendChild(settingsField(
      "白名单",
      textAreaInput("whitelist", config.whitelist, "每行一个 UMO、群号或 QQ"),
      "命中的会话永不清理。支持完整 UMO、群号/QQ、GroupMessage:123。",
      { full: true }
    ));
    scopeGrid.appendChild(settingsField(
      "预览默认包含 provider_stats",
      checkInput("include_provider_stats", config.include_provider_stats),
      "调用统计默认不清理；开启后预览默认计入。",
      { switch: true, full: true }
    ));

    const autoGrid = settingsSection(
      form,
      "自动清理",
      "定时扫描并清理已退群会话；默认关闭，不影响已删好友/临时会话。"
    );
    autoGrid.appendChild(settingsField(
      "启用自动清理",
      checkInput("auto_clean_enabled", config.auto_clean_enabled),
      "开启后按下面的间隔自动扫描并清理已退群会话。",
      { switch: true, full: true }
    ));
    autoGrid.appendChild(settingsField(
      "自动清理包含 provider_stats",
      checkInput("auto_clean_include_provider_stats", config.auto_clean_include_provider_stats),
      "默认关闭；打开后自动清理会连调用统计一起删。",
      { switch: true, full: true }
    ));
    autoGrid.appendChild(settingsField(
      "自动扫描间隔（分钟）",
      numberInput("scan_interval_minutes", config.scan_interval_minutes, 0, 1440, 10),
      "0 表示不自动执行；开启自动清理后建议 60–1440 分钟。",
      { full: true }
    ));

    const notifyGrid = settingsSection(
      form,
      "执行后通知",
      "自动清理删除数据后发送摘要；通知失败不影响清理。"
    );
    notifyGrid.appendChild(settingsField(
      "启用通知",
      checkInput("notify_enabled", config.notify_enabled),
      "删除数据后发送摘要；通知失败只记日志。",
      { switch: true, full: true }
    ));
    notifyGrid.appendChild(settingsField(
      "通知目标",
      textAreaInput("notify_targets", config.notify_targets, "每行一个群聊或私聊 UMO"),
      "留空时发送给 AstrBot admins_id 里的数字管理员 QQ。",
      { full: true }
    ));

    warningList($("settings-notes"), data.notes);
    await loadAutomationStatus();
  }

  async function loadAutomationStatus() {
    const box = $("automation-status");
    if (!box) return;
    box.replaceChildren(el("p", "muted", "正在读取自动清理状态…"));
    try {
      const data = await apiGet("automation_status");
      if (!data.ok) {
        box.replaceChildren(el("p", "error-text", data.error || "读取自动清理状态失败"));
        return;
      }
      const status = data.status || {};
      box.replaceChildren();
      const head = el("div", "card-head");
      const copy = el("div");
      copy.appendChild(el("h2", "", "自动清理状态"));
      copy.appendChild(el("p", "muted", "只处理已退群会话；默认不含 provider_stats。"));
      head.appendChild(copy);
      head.appendChild(el(
        "span",
        "pill " + (status.enabled ? "pill-on" : "pill-off"),
        status.enabled ? "已开启" : "已关闭"
      ));
      box.appendChild(head);

      const grid = el("div", "automation-grid");
      grid.appendChild(automationItem(
        "扫描间隔",
        status.interval_minutes > 0 ? "每 " + fmtNum(status.interval_minutes) + " 分钟" : "未设置"
      ));
      grid.appendChild(automationItem(
        "下次运行",
        status.next_run ? fmtTime(status.next_run) : "--"
      ));
      grid.appendChild(automationItem(
        "上次运行",
        status.last_run ? fmtTime(status.last_run) : "尚未运行"
      ));
      let lastText = "--";
      let lastKind = "";
      const last = status.last_result;
      if (last) {
        if (last.ok && last.skipped) {
          lastText = "无候选，已跳过";
          lastKind = "ok";
        } else if (last.ok) {
          lastText = "删除 " + fmtNum((last.result || {}).deleted) + " 行";
          lastKind = "ok";
        } else {
          lastText = last.error || "失败";
          lastKind = "bad";
        }
      }
      grid.appendChild(automationItem("上次结果", lastText, lastKind));
      box.appendChild(grid);
      box.appendChild(el(
        "p",
        "automation-foot",
        "范围：只处理已退群会话；不动已删好友/临时会话；" +
          (status.include_provider_stats ? "包含 provider_stats。" : "不含 provider_stats。")
      ));
    } catch (error) {
      box.replaceChildren(el("p", "error-text", error && error.message ? error.message : String(error)));
    }
  }

  function automationItem(label, value, kind) {
    const item = el("div", "automation-item");
    item.appendChild(el("span", "", label));
    item.appendChild(el("strong", kind ? "automation-" + kind : "", value));
    return item;
  }

  async function saveSettings() {
    const form = $("settings-form");
    if (!form) return;
    const read = function (name) {
      const field = form.elements[name];
      return field ? String(field.value || "").trim() : "";
    };
    const checked = function (name) {
      const field = form.elements[name];
      return Boolean(field && field.checked);
    };
    const list = function (name) {
      return read(name).split(/[\n,，]+/).map(function (item) {
        return item.trim();
      }).filter(Boolean);
    };
    const button = $("settings-save");
    setBusy(button, true, "保存中…");
    setInlineText("settings-status", "");
    try {
      const data = await apiPost("settings_save", {
        platform_id: read("platform_id") || "default",
        temp_session_days: Number(read("temp_session_days")),
        scan_timeout_seconds: Number(read("scan_timeout_seconds")),
        backup_retain: Number(read("backup_retain")),
        include_provider_stats: checked("include_provider_stats"),
        whitelist: list("whitelist"),
        auto_clean_enabled: checked("auto_clean_enabled"),
        scan_interval_minutes: Number(read("scan_interval_minutes")),
        auto_clean_include_provider_stats: checked("auto_clean_include_provider_stats"),
        notify_enabled: checked("notify_enabled"),
        notify_targets: list("notify_targets"),
      });
      if (!data.ok) {
        setInlineText("settings-status", data.error || "保存失败", "error");
        return;
      }
      await loadSettings();
      setInlineText("settings-status", data.message || "配置已保存", "success");
    } catch (error) {
      setInlineText("settings-status", error && error.message ? error.message : String(error), "error");
    } finally {
      setBusy(button, false, "保存设置");
    }
  }

  // ------------------------------------------------------------------
  // 交互
  // ------------------------------------------------------------------

  async function triggerScan() {
    const button = $("scan-btn");
    setBusy(button, true, "扫描中…");
    setInlineText("global-status", "正在读取群/好友列表并扫描核心库…");
    hideBanner();
    try {
      const data = await apiPost("scan", {});
      if (!data.ok) {
        setInlineText("global-status", data.error || "扫描失败", "error");
        showBanner(data.error || "扫描失败", "error");
        return;
      }
      await loadOverview();
      const candidates = fmtNum((data.scan.summary || {}).candidates);
      setInlineText("global-status", "扫描完成，候选 " + candidates + " 个会话 · " + new Date().toLocaleTimeString(), "success");
    } catch (error) {
      setInlineText("global-status", error && error.message ? error.message : String(error), "error");
      showBanner(error && error.message ? error.message : String(error), "error");
    } finally {
      setBusy(button, false, "开始扫描");
    }
  }

  async function applySessionFilter(button) {
    if (button) setBusy(button, true, "筛选中…");
    setInlineText("filter-feedback", "正在按当前条件筛选…");
    hideBanner();
    try {
      const data = await loadSessions();
      if (!data) return;
      setInlineText(
        "filter-feedback",
        "筛选完成：共 " + fmtNum(data.total) + " 个会话 · " + new Date().toLocaleTimeString(),
        "success"
      );
    } catch (error) {
      setInlineText("filter-feedback", error && error.message ? error.message : String(error), "error");
      showBanner(error && error.message ? error.message : String(error), "error");
    } finally {
      if (button) setBusy(button, false, "筛选");
    }
  }

  async function refreshPreview(button) {
    if (button) setBusy(button, true, "生成中…");
    setInlineText("preview-status", "正在按当前开关重新计算预览…");
    hideBanner();
    try {
      const preview = await loadPreview();
      if (!preview) return;
      setInlineText(
        "preview-status",
        "预览已更新：候选 " + fmtNum(preview.summary.targets) +
          " 个 · 将处理 " + fmtNum(preview.summary.total_rows) +
          " 行 · " + new Date().toLocaleTimeString(),
        "success"
      );
    } catch (error) {
      setInlineText("preview-status", error && error.message ? error.message : String(error), "error");
      showBanner(error && error.message ? error.message : String(error), "error");
    } finally {
      if (button) setBusy(button, false, "重新生成预览");
    }
  }

  async function refreshCleanTargets(button) {
    if (button) setBusy(button, true, "读取中…");
    setInlineText("clean-targets-status", "正在重新读取最近一次扫描结果…");
    hideBanner();
    try {
      const data = await loadCleanTab();
      if (!data) return;
      setInlineText(
        "clean-targets-status",
        "候选已更新 · " + new Date().toLocaleTimeString(),
        "success"
      );
    } catch (error) {
      setInlineText("clean-targets-status", error && error.message ? error.message : String(error), "error");
      showBanner(error && error.message ? error.message : String(error), "error");
    } finally {
      if (button) setBusy(button, false, "重新读取候选");
    }
  }

  async function refreshBackups(button) {
    if (button) setBusy(button, true, "刷新中…");
    setInlineText("backups-status", "正在刷新备份列表…");
    try {
      const items = await loadBackups();
      if (!items) return;
      setInlineText(
        "backups-status",
        "备份列表已刷新：共 " + fmtNum(items.length) + " 份 · " + new Date().toLocaleTimeString(),
        "success"
      );
    } catch (error) {
      setInlineText("backups-status", error && error.message ? error.message : String(error), "error");
    } finally {
      if (button) setBusy(button, false, "刷新备份列表");
    }
  }

  async function refreshCurrent() {
    const button = $("refresh-btn");
    const names = {
      overview: "概览",
      sessions: "会话列表",
      preview: "清理预览",
      clean: "执行清理",
      logs: "扫描日志",
      settings: "设置",
    };
    setBusy(button, true, "刷新中…");
    setInlineText("global-status", "正在刷新" + (names[state.tab] || "当前页") + "…");
    hideBanner();
    try {
      if (state.tab === "overview") await loadOverview();
      else if (state.tab === "sessions") {
        const data = await loadSessions();
        if (!data) return;
      } else if (state.tab === "preview") {
        const preview = await loadPreview();
        if (!preview) return;
        setInlineText(
          "preview-status",
          "预览已刷新：候选 " + fmtNum(preview.summary.targets) +
            " 个 · " + new Date().toLocaleTimeString(),
          "success"
        );
      } else if (state.tab === "clean") {
        const data = await loadCleanTab();
        if (!data) return;
      } else if (state.tab === "logs") await loadLogs();
      else if (state.tab === "settings") await loadSettings();
      setInlineText(
        "global-status",
        (names[state.tab] || "当前页") + "已刷新 · " + new Date().toLocaleTimeString(),
        "success"
      );
    } catch (error) {
      setInlineText("global-status", error && error.message ? error.message : String(error), "error");
      showBanner(error && error.message ? error.message : String(error), "error");
    } finally {
      setBusy(button, false, "刷新当前页");
    }
  }

  async function switchTab(name) {
    state.tab = name;
    document.querySelectorAll(".tab").forEach(function (button) {
      const active = button.dataset.tab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".panel").forEach(function (panel) {
      panel.classList.toggle("active", panel.id === "panel-" + name);
    });
    hideBanner();
    try {
      if (name === "overview" && !state.overview) await loadOverview();
      else if (name === "sessions" && !state.sessionsLoaded) await loadSessions();
      else if (name === "preview" && !state.previewLoaded) await loadPreview();
      else if (name === "clean" && !state.cleanLoaded) await loadCleanTab();
      else if (name === "logs" && !state.logsLoaded) await loadLogs();
      else if (name === "settings" && !state.settingsLoaded) await loadSettings();
    } catch (error) {
      showBanner(error && error.message ? error.message : String(error), "error");
    }
  }

  async function init() {
    document.querySelectorAll(".tab").forEach(function (button) {
      button.addEventListener("click", function () { switchTab(button.dataset.tab); });
    });
    $("scan-btn").addEventListener("click", triggerScan);
    $("refresh-btn").addEventListener("click", refreshCurrent);
    $("filter-btn").addEventListener("click", function () {
      applySessionFilter($("filter-btn"));
    });
    ["filter-status", "filter-type"].forEach(function (id) {
      $(id).addEventListener("change", function () {
        applySessionFilter(null);
      });
    });
    $("filter-q").addEventListener("keydown", function (event) {
      if (event.key === "Enter") applySessionFilter(null);
    });
    $("preview-btn").addEventListener("click", function () {
      refreshPreview($("preview-btn"));
    });
    $("include-provider-stats").addEventListener("change", function () {
      refreshPreview(null);
    });
    $("clean-btn").addEventListener("click", executeClean);
    $("clean-include-provider-stats").addEventListener("change", syncProviderOnlySelection);
    $("show-provider-only").addEventListener("change", applyCleanVisibility);
    $("clean-confirm").addEventListener("input", function () {
      setInlineText("clean-error", "");
    });
    $("clean-refresh").addEventListener("click", function () {
      refreshCleanTargets($("clean-refresh"));
    });
    $("backups-refresh").addEventListener("click", function () {
      refreshBackups($("backups-refresh"));
    });
    $("settings-save").addEventListener("click", saveSettings);
    $("settings-form").addEventListener("submit", function (event) {
      event.preventDefault();
      saveSettings();
    });
    $("settings-reload").addEventListener("click", function () {
      setInlineText("settings-status", "");
      loadSettings().catch(function (error) { showBanner(String(error.message || error)); });
    });
    $("detail-close").addEventListener("click", closeDetail);
    $("detail-modal").addEventListener("click", function (event) {
      if (event.target && event.target.dataset && event.target.dataset.close === "detail") {
        closeDetail();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !$("detail-modal").classList.contains("hidden")) {
        closeDetail();
      }
    });

    const bridge = await waitForBridge();
    if (!bridge) {
      showBanner("插件页面桥接不可用，请从 AstrBot 插件页面打开。", "error");
      return;
    }
    try {
      await loadOverview();
    } catch (error) {
      showBanner(error && error.message ? error.message : String(error), "error");
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
