(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    dateInput: $("dateInput"),
    todayBtn: $("todayBtn"),
    dateHint: $("dateHint"),
    searchControl: $("searchControl"),
    searchInput: $("searchInput"),
    searchBtn: $("searchBtn"),
    clearSearchBtn: $("clearSearchBtn"),
    searchHint: $("searchHint"),
    refreshBtn: $("refreshBtn"),
    statusDot: $("statusDot"),
    statusText: $("statusText"),
    updatedAt: $("updatedAt"),
    statTotal: $("statTotal"),
    statTotalFoot: $("statTotalFoot"),
    statRounds: $("statRounds"),
    statRoundsFoot: $("statRoundsFoot"),
    statFilter: $("statFilter"),
    statFilterFoot: $("statFilterFoot"),
    statSource: $("statSource"),
    statSourceFoot: $("statSourceFoot"),
    chartPanel: $("chartPanel"),
    chartTitle: $("chartTitle"),
    chartSubtitle: $("chartSubtitle"),
    chartModeSwitch: $("chartModeSwitch"),
    chartOpenBtn: $("chartOpenBtn"),
    chartThumb: $("chartThumb"),
    chartSmall: $("chartSmall"),
    chartModal: $("chartModal"),
    chartModalTitle: $("chartModalTitle"),
    chartModalSubtitle: $("chartModalSubtitle"),
    chartModalScroll: $("chartModalScroll"),
    chartModalCanvas: $("chartModalCanvas"),
    chartZoomOut: $("chartZoomOut"),
    chartZoomIn: $("chartZoomIn"),
    chartZoomReset: $("chartZoomReset"),
    chartZoomLabel: $("chartZoomLabel"),
    chartClose: $("chartClose"),
    imageModal: $("imageModal"),
    imageModalTitle: $("imageModalTitle"),
    imageModalSubtitle: $("imageModalSubtitle"),
    imageModalScroll: $("imageModalScroll"),
    imagePreview: $("imagePreview"),
    imageError: $("imageError"),
    imageZoomOut: $("imageZoomOut"),
    imageZoomIn: $("imageZoomIn"),
    imageZoomReset: $("imageZoomReset"),
    imageZoomLabel: $("imageZoomLabel"),
    imageClose: $("imageClose"),
    notice: $("notice"),
    results: $("results"),
    pagination: $("pagination"),
    pageSummary: $("pageSummary"),
    pager: $("pager"),
    pageSizeSelect: $("pageSizeSelect"),
  };

  const state = {
    today: null,
    minDate: null,
    date: null,
    page: 1,
    pageSize: 50,
    search: "",
    data: null,
    loading: false,
    controller: null,
    requestId: 0,
    streaming: null,
    chartMode: "device",
    chartZoom: 1,
    chartModalOpen: false,
    chartModalSvgs: [],
    chartModalBaseWidth: 1800,
    chartModalBaseHeight: 400,
    chartLastFocus: null,
    imageOpen: false,
    imageZoom: 1,
    imageBaseWidth: 0,
    imageBaseHeight: 0,
    imageLastFocus: null,
  };

  const CHART_SMALL_TOP = 50;
  const CHART_MODAL_TOP = 50;
  const SVG_NS = "http://www.w3.org/2000/svg";

  const numberFormat = new Intl.NumberFormat("zh-CN");

  function formatNumber(value) {
    return numberFormat.format(Number(value) || 0);
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function setText(node, text) {
    node.textContent = text === undefined || text === null ? "—" : String(text);
  }

  function setStatus(kind, text, updatedAt) {
    els.statusDot.className = "status-dot" + (kind ? ` ${kind}` : "");
    els.statusText.textContent = text;
    els.updatedAt.textContent = updatedAt ? `更新 ${updatedAt}` : "—";
  }

  function syncSearchState() {
    const isToday = state.date === state.today;
    els.searchInput.disabled = !isToday;
    els.searchBtn.disabled = !isToday || state.loading;
    els.clearSearchBtn.disabled = !isToday || !els.searchInput.value;
    els.searchControl.classList.toggle("is-disabled", !isToday);
    els.searchHint.classList.toggle("warn", !isToday);
    els.searchHint.textContent = isToday
      ? "仅搜索当天结果，支持通道编码子串匹配"
      : "历史日期不支持按通道编码搜索";
  }

  function setLoading(loading) {
    state.loading = loading;
    els.refreshBtn.disabled = loading;
    els.refreshBtn.classList.toggle("is-loading", loading);
    els.searchBtn.disabled = loading || state.date !== state.today;
    els.pageSizeSelect.disabled = loading;
    els.results.classList.toggle("is-loading", loading && Boolean(state.data));
  }

  async function readJson(response) {
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("需要登录");
    }
    if (!response.ok) {
      let detail = "";
      try {
        const payload = await response.json();
        detail = payload && payload.detail ? payload.detail : "";
      } catch {
        /* ignore malformed error body */
      }
      throw new Error(detail || `请求失败（HTTP ${response.status}）`);
    }
    return response.json();
  }

  async function fetchJson(url) {
    const response = await fetch(url, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    return readJson(response);
  }

  async function init() {
    els.pageSizeSelect.value = String(state.pageSize);
    bindEvents();

    try {
      const dates = await fetchJson("/api/dashboard/dates");
      state.today = dates.today;
      state.minDate = dates.minDate || dates.today;

      const params = new URLSearchParams(window.location.search);
      const requestedDate = params.get("date");
      state.date = requestedDate &&
        requestedDate >= state.minDate && requestedDate <= state.today
        ? requestedDate
        : state.today;

      const requestedPage = Number(params.get("page"));
      state.page = Number.isInteger(requestedPage) && requestedPage > 0
        ? requestedPage
        : 1;

      if (state.date === state.today) {
        const requestedSearch = (params.get("q") || "").trim();
        if (requestedSearch) {
          state.search = requestedSearch;
          els.searchInput.value = requestedSearch;
        }
      }

      els.dateInput.min = state.minDate;
      els.dateInput.max = state.today;
      els.dateInput.value = state.date;

      const dayCount = Array.isArray(dates.items) ? dates.items.length : 1;
      els.dateHint.textContent = state.minDate === state.today
        ? `当前仅可查询今天（${state.today}）`
        : `可查询 ${state.minDate} ～ ${state.today} · 共 ${dayCount} 天`;

      syncSearchState();
      await loadResults();
    } catch (error) {
      setStatus("error", "读取失败");
      renderError(error);
    }
  }

  function bindEvents() {
    els.dateInput.addEventListener("change", () => {
      const chosen = els.dateInput.value;
      if (!chosen) return;
      state.date = chosen;
      state.page = 1;
      if (chosen !== state.today) {
        state.search = "";
        els.searchInput.value = "";
      }
      syncSearchState();
      loadResults();
    });

    els.todayBtn.addEventListener("click", () => {
      if (!state.today) return;
      state.date = state.today;
      els.dateInput.value = state.today;
      state.page = 1;
      state.search = "";
      els.searchInput.value = "";
      syncSearchState();
      loadResults();
    });

    els.searchBtn.addEventListener("click", applySearch);
    els.searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        applySearch();
      }
    });
    els.searchInput.addEventListener("input", () => {
      els.clearSearchBtn.disabled =
        state.date !== state.today || !els.searchInput.value;
    });

    els.clearSearchBtn.addEventListener("click", () => {
      if (state.date !== state.today) return;
      els.searchInput.value = "";
      els.clearSearchBtn.disabled = true;
      if (state.search) {
        state.search = "";
        state.page = 1;
        loadResults();
      }
    });

    els.refreshBtn.addEventListener("click", () => loadResults());

    els.pageSizeSelect.addEventListener("change", () => {
      state.pageSize = Number(els.pageSizeSelect.value) || 50;
      state.page = 1;
      loadResults();
    });

    els.chartModeSwitch.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-chart-mode]");
      if (!button) return;
      setChartMode(button.dataset.chartMode);
    });

    els.chartThumb.addEventListener("click", openChartModal);
    els.chartOpenBtn.addEventListener("click", openChartModal);
    els.chartClose.addEventListener("click", closeChartModal);
    els.chartModal.addEventListener("click", (event) => {
      if (event.target.closest("[data-chart-close]")) closeChartModal();
    });
    els.chartZoomIn.addEventListener("click", () => setChartZoom(state.chartZoom + 0.25));
    els.chartZoomOut.addEventListener("click", () => setChartZoom(state.chartZoom - 0.25));
    els.chartZoomReset.addEventListener("click", () => setChartZoom(1));

    els.chartModalScroll.addEventListener("wheel", (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      setChartZoom(state.chartZoom + (event.deltaY < 0 ? 0.15 : -0.15));
    }, { passive: false });

    let pinchStartDistance = 0;
    let pinchStartZoom = 1;
    els.chartModalScroll.addEventListener("touchstart", (event) => {
      if (event.touches.length !== 2) return;
      pinchStartDistance = touchDistance(event.touches);
      pinchStartZoom = state.chartZoom;
    }, { passive: true });
    els.chartModalScroll.addEventListener("touchmove", (event) => {
      if (event.touches.length !== 2 || !pinchStartDistance) return;
      event.preventDefault();
      const ratio = touchDistance(event.touches) / pinchStartDistance;
      setChartZoom(pinchStartZoom * ratio);
    }, { passive: false });
    els.chartModalScroll.addEventListener("touchend", () => {
      pinchStartDistance = 0;
    }, { passive: true });

    els.imageClose.addEventListener("click", closeImageModal);
    els.imageModal.addEventListener("click", (event) => {
      if (event.target.closest("[data-image-close]")) closeImageModal();
    });
    els.imageZoomIn.addEventListener("click", () => setImageZoom(state.imageZoom + 0.25));
    els.imageZoomOut.addEventListener("click", () => setImageZoom(state.imageZoom - 0.25));
    els.imageZoomReset.addEventListener("click", () => setImageZoom(1));
    els.imagePreview.addEventListener("load", fitImageToViewport);
    els.imagePreview.addEventListener("error", () => {
      els.imageError.classList.remove("hidden");
      els.imagePreview.classList.add("hidden");
      els.imageZoomLabel.textContent = "—";
    });

    els.imageModalScroll.addEventListener("wheel", (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      setImageZoom(state.imageZoom + (event.deltaY < 0 ? 0.15 : -0.15));
    }, { passive: false });

    let imagePinchStartDistance = 0;
    let imagePinchStartZoom = 1;
    els.imageModalScroll.addEventListener("touchstart", (event) => {
      if (event.touches.length !== 2) return;
      imagePinchStartDistance = touchDistance(event.touches);
      imagePinchStartZoom = state.imageZoom;
    }, { passive: true });
    els.imageModalScroll.addEventListener("touchmove", (event) => {
      if (event.touches.length !== 2 || !imagePinchStartDistance) return;
      event.preventDefault();
      setImageZoom(
        imagePinchStartZoom *
        (touchDistance(event.touches) / imagePinchStartDistance)
      );
    }, { passive: false });
    els.imageModalScroll.addEventListener("touchend", () => {
      imagePinchStartDistance = 0;
    }, { passive: true });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (state.imageOpen) {
        closeImageModal();
      } else if (state.chartModalOpen) {
        closeChartModal();
      }
    });

    els.pager.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-page]");
      if (!button || button.disabled) return;
      const page = Number(button.dataset.page);
      if (!Number.isFinite(page) || page === state.page) return;
      state.page = page;
      loadResults();
      scrollToResults();
    });

    els.results.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      if (button.dataset.action === "retry") {
        loadResults();
      } else if (button.dataset.action === "clear-search") {
        els.searchInput.value = "";
        state.search = "";
        state.page = 1;
        syncSearchState();
        loadResults();
      }
    });
  }

  function applySearch() {
    if (state.date !== state.today) return;
    const query = els.searchInput.value.trim();
    if (query === state.search) {
      if (state.page !== 1) {
        state.page = 1;
        loadResults();
      } else {
        loadResults();
      }
      return;
    }
    state.search = query;
    state.page = 1;
    syncSearchState();
    loadResults();
  }

  async function loadResults() {
    if (!state.date) return;

    const requestId = ++state.requestId;
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    setLoading(true);

    const params = new URLSearchParams({
      date: state.date,
      page: String(state.page),
      page_size: String(state.pageSize),
      order: "desc",
    });
    if (state.search && state.date === state.today) {
      params.set("channel_code", state.search);
    }

    try {
      const response = await fetch(`/api/dashboard/results?${params}`, {
        cache: "no-store",
        signal: state.controller.signal,
        headers: { Accept: "application/json" },
      });
      const data = await readJson(response);
      if (requestId !== state.requestId) return;

      state.data = data;
      state.page = data.page;
      state.pageSize = data.pageSize;
      els.pageSizeSelect.value = String(data.pageSize);

      render(data);
      syncUrl(data);
      setStatus(
        data.isToday ? "live" : "history",
        data.isToday ? "今日数据" : "历史归档",
        data.updatedAt
      );
    } catch (error) {
      if (error.name === "AbortError") return;
      if (requestId !== state.requestId) return;
      setStatus("error", "读取失败");
      renderError(error);
    } finally {
      if (requestId === state.requestId) {
        setLoading(false);
        syncSearchState();
      }
    }
  }

  function render(data) {
    renderStats(data);
    renderChart(data);
    renderNotice(data);

    els.results.replaceChildren();
    if (!data.groups || data.groups.length === 0) {
      els.results.append(buildEmpty(data));
    } else {
      for (const group of data.groups) {
        els.results.append(buildRound(group, data));
      }
    }
    renderPagination(data);
  }

  function renderStats(data) {
    setText(els.statTotal, formatNumber(data.totalMatched));
    if (data.search) {
      els.statTotalFoot.textContent =
        `匹配 / 当天共 ${formatNumber(data.total)} 条`;
    } else if (data.isToday) {
      els.statTotalFoot.textContent = "当天异常记录";
    } else {
      els.statTotalFoot.textContent = `${data.date} 归档记录`;
    }

    setText(
      els.statRounds,
      data.roundSource === "none" ? "—" : formatNumber(data.matchedRoundCount)
    );
    if (data.search) {
      els.statRoundsFoot.textContent =
        `共 ${formatNumber(data.roundCount)} 轮有记录`;
    } else if (data.isToday) {
      els.statRoundsFoot.textContent = "今日已记录轮次";
    } else if (data.roundSource === "inferred") {
      els.statRoundsFoot.textContent = "轮次为归档推断";
    } else {
      els.statRoundsFoot.textContent = "归档未含轮次信息";
    }

    setText(els.statFilter, data.search || "全部通道");
    els.statFilter.title = data.search || "全部通道";
    els.statFilterFoot.textContent = data.search
      ? "仅当天范围内"
      : "未启用搜索";

    setText(els.statSource, data.isToday ? "实时文件" : "历史归档");
    const files = data.source && Array.isArray(data.source.files)
      ? data.source.files
      : [];
    if (files.length === 0) {
      els.statSourceFoot.textContent = "暂无数据文件";
      els.statSourceFoot.removeAttribute("title");
    } else {
      const names = files.map((file) => String(file).split("/").pop());
      els.statSourceFoot.textContent = names.slice(0, 2).join(" · ") +
        (names.length > 2 ? ` +${names.length - 2}` : "");
      els.statSourceFoot.title = files.join("\n");
    }
  }

  function renderChart(data) {
    const streaming = data && data.streaming;
    state.streaming = streaming && Number(streaming.totalRows) > 0
      ? streaming
      : null;

    if (!state.streaming) {
      els.chartPanel.classList.add("hidden");
      if (state.chartModalOpen) closeChartModal();
      return;
    }

    els.chartPanel.classList.remove("hidden");
    updateChartModeButtons();
    renderSmallChart(data);
    if (state.chartModalOpen) renderModalChart(data);
  }

  function chartSeries() {
    if (!state.streaming) return [];
    const series = state.streaming.source;
    return Array.isArray(series) ? series : [];
  }

  function chartTitleText(data, topN) {
    const scope = data && data.isToday ? "今日" : (data ? data.date : "当日");
    const title = state.chartMode === "device" ? "设备名称" : "发生通道";
    return `${scope}${title} Top ${topN}`;
  }

  function chartSubtitleText() {
    if (!state.streaming) return "";
    const display = state.chartMode === "device"
      ? "当前横轴：设备名称"
      : "当前横轴：通道编码";
    return `按 MISMATCH 通道编码聚合 · 共 ${formatNumber(state.streaming.mismatchCount || 0)} 次 MISMATCH · 当日 ${formatNumber(state.streaming.totalRows)} 条异常 · ${display}`;
  }

  function updateChartModeButtons() {
    for (const button of els.chartModeSwitch.querySelectorAll("button[data-chart-mode]")) {
      const active = button.dataset.chartMode === state.chartMode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    }
  }

  function setChartMode(mode) {
    if (mode !== "device" && mode !== "channel") return;
    if (mode === state.chartMode) return;
    state.chartMode = mode;
    updateChartModeButtons();
    if (state.streaming && state.data) {
      renderSmallChart(state.data);
      if (state.chartModalOpen) renderModalChart(state.data);
    }
  }

  function renderSmallChart(data) {
    const series = chartSeries().slice(0, CHART_SMALL_TOP);
    const topN = Math.max(1, Math.min(CHART_SMALL_TOP, series.length || CHART_SMALL_TOP));
    els.chartTitle.textContent = chartTitleText(data, topN);
    els.chartSubtitle.textContent = chartSubtitleText();
    els.chartSmall.replaceChildren();
    els.chartOpenBtn.disabled = series.length === 0;
    els.chartThumb.disabled = series.length === 0;

    if (series.length === 0) {
      els.chartSmall.append(
        element("div", "chart-empty", "当前条件下暂未发现 MISMATCH 通道")
      );
      return;
    }

    const labelKey = state.chartMode === "device" ? "deviceName" : "name";
    const xTitle = state.chartMode === "device" ? "设备名称" : "通道编码";

    els.chartSmall.append(buildChartSvg(series, {
      id: `small-${labelKey}`,
      width: 1800,
      height: 400,
      labelChars: 12,
      labelHeadRatio: labelKey === "deviceName" ? 0.6 : 0.3,
      labelEvery: 5,
      maxBarWidth: 34,
      labelKey,
      xTitle,
      yTitle: "MISMATCH 次数",
      axisTitleSize: 18,
      gradientFrom: "#3b82f6",
      gradientTo: "#0e7490",
    }));
  }

  function buildChartSvg(series, options) {
    const width = options.width;
    const height = options.height;
    const margin = options.id === "modal"
      ? { top: 42, right: 36, bottom: 150, left: 82 }
      : { top: 26, right: 18, bottom: 100, left: 64 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const baseline = margin.top + plotHeight;
    const maxCount = Math.max(1, ...series.map((item) => Number(item.count) || 0));
    const step = niceStep(maxCount / 4);
    const yMax = Math.max(step, Math.ceil(maxCount / step) * step);
    const slot = plotWidth / Math.max(1, series.length);
    const barWidth = Math.min(options.maxBarWidth, slot * 0.62);
    const gradientId = `chart-gradient-${options.id}`;

    const svg = svgElement("svg", {
      viewBox: `0 0 ${width} ${height}`,
      class: "chart-svg",
      role: "img",
      "aria-label": options.labelKey === "deviceName"
        ? "对应设备名称柱状图，横轴为设备名称，纵轴为 MISMATCH 次数"
        : "发生通道柱状图，横轴为通道编码，纵轴为 MISMATCH 次数",
      preserveAspectRatio: "xMidYMid meet",
    });

    const defs = svgElement("defs");
    const gradient = svgElement("linearGradient", {
      id: gradientId,
      x1: "0",
      y1: "0",
      x2: "0",
      y2: "1",
    });
    gradient.append(
      svgElement("stop", {
        offset: "0",
        "stop-color": options.gradientFrom || "#3b82f6",
      }),
      svgElement("stop", {
        offset: "1",
        "stop-color": options.gradientTo || "#0e7490",
      })
    );
    defs.append(gradient);
    svg.append(defs);

    for (let value = 0; value <= yMax + 0.0001; value += step) {
      const y = baseline - (value / yMax) * plotHeight;
      svg.append(svgElement("line", {
        x1: margin.left,
        y1: y,
        x2: margin.left + plotWidth,
        y2: y,
        class: "chart-grid",
      }));
      const label = svgElement("text", {
        x: margin.left - 10,
        y: y + 4,
        class: "chart-axis-text",
        "text-anchor": "end",
      });
      label.textContent = formatNumber(Math.round(value));
      svg.append(label);
    }

    svg.append(
      svgElement("line", {
        x1: margin.left,
        y1: margin.top,
        x2: margin.left,
        y2: baseline,
        class: "chart-axis",
      }),
      svgElement("line", {
        x1: margin.left,
        y1: baseline,
        x2: margin.left + plotWidth,
        y2: baseline,
        class: "chart-axis",
      })
    );

    const axisTitleSize = options.axisTitleSize || 13;
    const yTitle = svgElement("text", {
      x: 18,
      y: margin.top + plotHeight / 2,
      class: "chart-axis-title",
      "text-anchor": "middle",
      transform: `rotate(-90 18 ${margin.top + plotHeight / 2})`,
    });
    yTitle.style.fontSize = `${axisTitleSize}px`;
    yTitle.textContent = options.yTitle || "MISMATCH 次数";
    svg.append(yTitle);

    series.forEach((item, index) => {
      const count = Number(item.count) || 0;
      const barHeight = (count / yMax) * plotHeight;
      const x = margin.left + slot * index + (slot - barWidth) / 2;
      const y = baseline - barHeight;
      const centerX = x + barWidth / 2;

      const bar = svgElement("rect", {
        x,
        y,
        width: barWidth,
        height: Math.max(1, barHeight),
        rx: 4,
        ry: 4,
        class: "chart-bar",
        fill: `url(#${gradientId})`,
      });
      const labelValue = options.labelKey === "deviceName"
        ? (item.deviceName || item.name)
        : item.name;
      const tip = svgElement("title");
      tip.textContent = options.labelKey === "deviceName"
        ? `设备 ${labelValue} · 通道 ${item.name}：${formatNumber(count)} 次`
        : `${item.name}（设备 ${item.deviceName || "—"}）：${formatNumber(count)} 次`;
      bar.append(tip);
      svg.append(bar);

      const value = svgElement("text", {
        x: centerX,
        y: Math.max(margin.top + 12, y - 7),
        class: "chart-value",
        "text-anchor": "middle",
      });
      value.textContent = formatNumber(count);
      svg.append(value);

      const labelY = baseline + 16;
      const label = svgElement("text", {
        x: centerX,
        y: labelY,
        class: "chart-label",
        "data-label-index": index,
        "text-anchor": "end",
        transform: `rotate(-38 ${centerX} ${labelY})`,
      });
      label.textContent = truncateLabel(
        labelValue, options.labelChars, options.labelHeadRatio
      );
      const labelTip = svgElement("title");
      labelTip.textContent = labelValue;
      label.append(labelTip);
      svg.append(label);
    });

    const xTitle = svgElement("text", {
      x: margin.left + plotWidth / 2,
      y: height - 18,
      class: "chart-axis-title",
      "text-anchor": "middle",
    });
    xTitle.style.fontSize = `${axisTitleSize}px`;
    xTitle.textContent = options.xTitle || "设备名称";
    svg.append(xTitle);

    setChartLabelStep(svg, options.labelEvery || 1);
    return svg;
  }

  function setChartLabelStep(svg, step) {
    for (const label of svg.querySelectorAll("text.chart-label")) {
      const index = Number(label.dataset.labelIndex);
      label.style.display = index % step === 0 ? "" : "none";
    }
  }

  function svgElement(tag, attributes = {}) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attributes)) {
      node.setAttribute(key, String(value));
    }
    return node;
  }

  function niceStep(raw) {
    if (!Number.isFinite(raw) || raw <= 0) return 1;
    const exponent = Math.floor(Math.log10(raw));
    const fraction = raw / (10 ** exponent);
    let nice;
    if (fraction <= 1) nice = 1;
    else if (fraction <= 2) nice = 2;
    else if (fraction <= 5) nice = 5;
    else nice = 10;
    return Math.max(1, nice * (10 ** exponent));
  }

  function truncateLabel(text, maxChars, headRatio = 0.6) {
    const value = String(text || "—");
    const chars = Array.from(value);
    if (chars.length <= maxChars) return value;
    const headLength = Math.max(1, Math.floor((maxChars - 1) * headRatio));
    const tailLength = Math.max(1, maxChars - 1 - headLength);
    return `${chars.slice(0, headLength).join("")}…${chars.slice(-tailLength).join("")}`;
  }

  function openChartModal() {
    if (!state.streaming || chartSeries().length === 0) return;
    if (state.imageOpen) closeImageModal();
    state.chartLastFocus = document.activeElement;
    state.chartModalOpen = true;
    state.chartZoom = 1;
    els.chartModal.classList.remove("hidden");
    document.body.classList.add("chart-modal-open");
    renderModalChart(state.data);
    requestAnimationFrame(() => els.chartClose.focus());
  }

  function closeChartModal() {
    if (!state.chartModalOpen) return;
    state.chartModalOpen = false;
    state.chartModalSvgs = [];
    els.chartModal.classList.add("hidden");
    document.body.classList.remove("chart-modal-open");
    if (state.chartLastFocus && typeof state.chartLastFocus.focus === "function") {
      state.chartLastFocus.focus();
    }
    state.chartLastFocus = null;
  }

  function renderModalChart(data) {
    if (!data || !state.streaming) return;
    const series = chartSeries().slice(0, CHART_MODAL_TOP);
    const topN = Math.max(1, Math.min(CHART_MODAL_TOP, series.length || CHART_MODAL_TOP));
    els.chartModalTitle.textContent = chartTitleText(data, topN);
    els.chartModalSubtitle.textContent = chartSubtitleText();
    els.chartModalCanvas.replaceChildren();

    if (series.length === 0) {
      state.chartModalSvgs = [];
      els.chartModalCanvas.append(
        element("div", "chart-empty", "当前条件下暂未发现 MISMATCH 通道")
      );
      updateChartZoomLabel();
      return;
    }

    const availableWidth = Math.max(
      300,
      (els.chartModalScroll.clientWidth || 1200) - 36
    );
    const baseWidth = Math.min(2400, availableWidth);
    state.chartModalBaseWidth = baseWidth;
    state.chartModalBaseHeight = Math.round(baseWidth * (520 / 2400));

    const labelKey = state.chartMode === "device" ? "deviceName" : "name";
    const xTitle = state.chartMode === "device" ? "设备名称" : "通道编码";

    const svg = buildChartSvg(series, {
      id: `modal-${labelKey}`,
      width: 2400,
      height: 520,
      labelChars: 16,
      labelHeadRatio: labelKey === "deviceName" ? 0.6 : 0.3,
      labelEvery: 4,
      maxBarWidth: 28,
      labelKey,
      xTitle,
      yTitle: "MISMATCH 次数",
      axisTitleSize: 28,
      gradientFrom: "#3b82f6",
      gradientTo: "#0e7490",
    });

    state.chartModalSvgs = [svg];
    els.chartModalCanvas.append(svg);
    applyChartZoom();
  }

  function setChartZoom(next) {
    state.chartZoom = Math.min(
      4,
      Math.max(0.5, Math.round((Number(next) || 1) * 100) / 100)
    );
    applyChartZoom();
  }

  function applyChartZoom() {
    updateChartZoomLabel();
    const labelStep = state.chartZoom >= 1.5 ? 1 : 4;
    for (const svg of state.chartModalSvgs) {
      svg.style.width = `${state.chartModalBaseWidth * state.chartZoom}px`;
      svg.style.height = `${state.chartModalBaseHeight * state.chartZoom}px`;
      setChartLabelStep(svg, labelStep);
    }
  }

  function updateChartZoomLabel() {
    els.chartZoomLabel.textContent = `${Math.round(state.chartZoom * 100)}%`;
  }

  function frameUrl(row) {
    const params = new URLSearchParams({
      index: String(row.index),
      channel_code: row.channelCode || "",
      t: String(Date.now()),
    });
    if (row.frameDir) params.set("frame_dir", row.frameDir);
    return `/api/dashboard/frame?${params.toString()}`;
  }

  function openImageModal(row) {
    if (!row || !row.hasImage || row.index === null || row.index === undefined) {
      return;
    }
    if (state.chartModalOpen) closeChartModal();
    state.imageLastFocus = document.activeElement;
    state.imageOpen = true;
    state.imageZoom = 1;
    state.imageBaseWidth = 0;
    state.imageBaseHeight = 0;
    els.imageModalTitle.textContent = `抓帧图片 · ${row.name || row.channelCode}`;
    els.imageModalSubtitle.textContent =
      `${row.time} · 通道 ${row.channelCode} · 期望 ${row.expected || "—"} → 识别 ${row.detected || "—"} · ${row.status} · 本轮异常抓帧`;
    els.imageError.classList.add("hidden");
    els.imagePreview.classList.remove("hidden");
    els.imagePreview.alt = `通道 ${row.channelCode} 抓帧图片`;
    els.imagePreview.removeAttribute("src");
    els.imageModal.classList.remove("hidden");
    document.body.classList.add("chart-modal-open");
    els.imagePreview.src = frameUrl(row);
    requestAnimationFrame(() => els.imageClose.focus());
  }

  function closeImageModal() {
    if (!state.imageOpen) return;
    state.imageOpen = false;
    state.imageBaseWidth = 0;
    state.imageBaseHeight = 0;
    els.imageModal.classList.add("hidden");
    els.imagePreview.removeAttribute("src");
    els.imageError.classList.add("hidden");
    document.body.classList.remove("chart-modal-open");
    if (state.imageLastFocus && typeof state.imageLastFocus.focus === "function") {
      state.imageLastFocus.focus();
    }
    state.imageLastFocus = null;
  }

  function fitImageToViewport() {
    if (!state.imageOpen) return;
    const naturalWidth = els.imagePreview.naturalWidth;
    const naturalHeight = els.imagePreview.naturalHeight;
    if (!naturalWidth || !naturalHeight) return;

    const availableWidth = Math.max(
      200,
      (els.imageModalScroll.clientWidth || window.innerWidth * 0.9) - 28
    );
    const availableHeight = Math.max(200, window.innerHeight * 0.64);
    const scale = Math.min(
      availableWidth / naturalWidth,
      availableHeight / naturalHeight,
      1.5
    );
    state.imageBaseWidth = Math.max(1, Math.round(naturalWidth * scale));
    state.imageBaseHeight = Math.max(1, Math.round(naturalHeight * scale));
    els.imageError.classList.add("hidden");
    els.imagePreview.classList.remove("hidden");
    applyImageZoom();
  }

  function setImageZoom(next) {
    state.imageZoom = Math.min(
      5,
      Math.max(0.25, Math.round((Number(next) || 1) * 100) / 100)
    );
    applyImageZoom();
  }

  function applyImageZoom() {
    els.imageZoomLabel.textContent = state.imageBaseWidth
      ? `${Math.round(state.imageZoom * 100)}%`
      : "—";
    if (!state.imageBaseWidth || !state.imageBaseHeight) return;
    els.imagePreview.style.width =
      `${state.imageBaseWidth * state.imageZoom}px`;
    els.imagePreview.style.height =
      `${state.imageBaseHeight * state.imageZoom}px`;
  }

  function touchDistance(touches) {
    const dx = touches[0].clientX - touches[1].clientX;
    const dy = touches[0].clientY - touches[1].clientY;
    return Math.hypot(dx, dy);
  }

  function renderNotice(data) {
    if (!data.isToday && !data.totalMatched) {
      els.notice.replaceChildren();
      els.notice.classList.add("hidden");
      els.notice.classList.remove("warn");
      return;
    }

    const messages = [];
    let warn = false;

    if (!data.isToday) {
      if (data.roundSource === "inferred") {
        messages.push(
          "历史轮次根据 output/osd_rounds.csv 推断，用于将归档记录按轮次分隔，仅供参考。"
        );
      } else if (data.roundSource === "none") {
        messages.push("归档 CSV 不含轮次标记，以下记录按时间排列。");
        warn = true;
      }
    }

    if (data.search) {
      messages.push(`搜索范围仅限 ${data.today} 当天，关键词：${data.search}`);
    }

    if (data.inProgress && data.isToday) {
      messages.push(
        `最新轮次 #${data.inProgress.number} 仍在进行中，已记录 ${data.inProgress.rowCount} 条异常。`
      );
    }

    els.notice.replaceChildren();
    if (messages.length === 0) {
      els.notice.classList.add("hidden");
      els.notice.classList.remove("warn");
      return;
    }
    els.notice.textContent =
      messages.map((message) => message.replace(/[。；\s]+$/, "")).join("；") + "。";
    els.notice.classList.toggle("warn", warn);
    els.notice.classList.remove("hidden");
  }

  function buildRound(group, data) {
    const card = element("section", "round-card");
    const head = element("div", "round-head");
    const left = element("div", "round-left");
    const titleLine = element("div", "round-title-line");

    const kicker = element(
      "span",
      "round-kicker",
      group.displayNumber ? `第 ${group.displayNumber} 轮` : "归档记录"
    );
    titleLine.append(kicker);

    if (group.number !== null && group.number !== undefined) {
      titleLine.append(element("span", "round-number", `监控 #${group.number}`));
    }

    const tags = element("span", "round-tags");
    if (group.crossesMidnight) {
      tags.append(element("span", "tag warn", "跨天"));
    }
    if (group.inferred) {
      tags.append(element("span", "tag", "轮次推断"));
    }
    if (group.key === "history-unmatched") {
      tags.append(element("span", "tag warn", "未匹配轮次"));
    }
    if (group.continued) {
      tags.append(element("span", "tag accent", "续页"));
    }
    if (data.isToday && !group.done && !group.inProgress) {
      tags.append(element("span", "tag warn", "未完成"));
    }
    if (tags.childNodes.length > 0) titleLine.append(tags);
    left.append(titleLine);

    const timeParts = [];
    if (group.startedAt) {
      timeParts.push(`开始 ${group.startedAt}`);
    } else {
      timeParts.push("开始时间未知");
    }
    if (group.endedAt) {
      timeParts.push(`最后记录 ${group.endedAt.slice(11)}`);
    }
    left.append(element("p", "round-time", timeParts.join(" · ")));

    const right = element("div", "round-right");
    const scope = group.crossesMidnight ? "当日" : "本轮";
    let countText;
    if (data.search) {
      countText = `匹配 ${formatNumber(group.matchedTotal)} 条 / ${scope}共 ${formatNumber(group.roundTotal)} 条`;
    } else {
      countText = `${scope}共 ${formatNumber(group.roundTotal)} 条异常`;
    }
    right.append(element("span", "round-count", countText));

    if (group.done) {
      const parts = [];
      if (group.done.ok !== null && group.done.ok !== undefined &&
          group.done.channels) {
        parts.push(`${formatNumber(group.done.ok)}/${formatNumber(group.done.channels)} 正常`);
      }
      if (group.done.anomalies !== null && group.done.anomalies !== undefined) {
        parts.push(`${formatNumber(group.done.anomalies)} 条异常`);
      }
      if (group.done.timeoutsSkipped !== null &&
          group.done.timeoutsSkipped !== undefined) {
        parts.push(`${formatNumber(group.done.timeoutsSkipped)} 次取流超时`);
      }
      if (group.done.durationSeconds !== null &&
          group.done.durationSeconds !== undefined) {
        parts.push(formatDuration(group.done.durationSeconds));
      }
      if (group.uncertain) {
        parts.push(`${formatNumber(group.uncertain)} 条不确定`);
      }
      right.append(element("span", "round-summary", `本轮完成 · ${parts.join(" · ")}`));
    } else if (data.isToday && group.inProgress) {
      right.append(element("span", "round-state", "进行中"));
    } else if (data.isToday) {
      right.append(element("span", "round-summary", "本轮未写入完成汇总"));
    }

    head.append(left, right);

    const scroll = element("div", "table-scroll");
    const table = element("table", "result-table");
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    const columns = [
      ["col-time", "时间"],
      ["col-index", "编号"],
      ["col-code", "通道编码"],
      ["col-name", "通道名称"],
      ["col-expected", "期望"],
      ["col-detected", "识别"],
      ["col-confidence", "置信度"],
      ["col-status", "状态"],
      ["col-sample", "样例"],
    ];
    for (const [className, label] of columns) {
      const th = element("th", className, label);
      headRow.append(th);
    }
    thead.append(headRow);
    table.append(thead);

    const tbody = document.createElement("tbody");
    for (const row of group.rows) {
      tbody.append(buildRow(row, data));
    }
    table.append(tbody);
    scroll.append(table);
    card.append(head, scroll);
    return card;
  }

  function buildRow(row, data) {
    const tr = document.createElement("tr");

    const timeCell = element("td", "cell-time",
      row.time.slice(0, 10) === data.date ? row.time.slice(11) : row.time);
    timeCell.title = row.time;
    tr.append(timeCell);

    tr.append(element("td", "cell-index",
      row.index === null || row.index === undefined ? "—" : String(row.index)));

    const code = element("td", "mono cell-code", row.channelCode || "—");
    code.title = row.channelCode || "";
    tr.append(code);

    const name = element("td", "cell-name", row.name || "—");
    name.title = row.name || "";
    tr.append(name);

    tr.append(element("td", "cell-expected", row.expected || "—"));

    const detected = element("td", "cell-detected", row.detected || "—");
    if (!row.detected) detected.classList.add("is-empty");
    detected.title = row.detected || "";
    tr.append(detected);

    tr.append(buildConfidenceCell(row.confidence));
    tr.append(buildStatusCell(row.status));
    tr.append(buildSampleCell(row, data));
    return tr;
  }

  function buildSampleCell(row, data) {
    const td = document.createElement("td");
    td.className = "cell-sample";
    if (data.isToday && row.hasImage) {
      const button = element("button", "sample-link", "查看图片");
      button.type = "button";
      button.title = "查看该通道本轮异常抓帧";
      button.addEventListener("click", () => openImageModal(row));
      td.append(button);
    } else {
      const empty = element("span", "sample-empty", "—");
      if (!data.isToday) {
        empty.title = "历史归档不保存图片";
      } else if ((row.status || "").toUpperCase().startsWith("MISMATCH")) {
        empty.title = "该通道没有可用的抓帧图片";
      }
      td.append(empty);
    }
    return td;
  }

  function buildConfidenceCell(confidence) {
    const td = document.createElement("td");
    if (confidence === null || confidence === undefined ||
        !Number.isFinite(Number(confidence))) {
      td.append(element("span", "confidence-text", "—"));
      return td;
    }

    const value = Math.max(0, Math.min(1, Number(confidence)));
    const percent = value * 100;
    const wrap = element("div", "confidence");
    const track = element("div", "confidence-track");
    const fill = element("div", "confidence-fill");
    fill.style.width = `${percent.toFixed(1)}%`;
    fill.classList.toggle("low", percent < 80);
    track.append(fill);
    wrap.append(track);
    wrap.append(element(
      "span",
      "confidence-text",
      percent >= 99.95 ? "100%" : `${percent.toFixed(1)}%`
    ));
    td.append(wrap);
    return td;
  }

  function buildStatusCell(status) {
    const td = document.createElement("td");
    const cell = element("div", "status-cell");
    const full = status || "—";
    const splitAt = full.indexOf("(");
    const base = splitAt >= 0 ? full.slice(0, splitAt) : full;
    const reason = splitAt >= 0 ? full.slice(splitAt) : "";

    const pill = element("span", `pill ${statusTone(base)}`, base);
    pill.title = full;
    cell.append(pill);

    if (reason) {
      const reasonNode = element("span", "status-reason", reason);
      reasonNode.title = full;
      cell.append(reasonNode);
    }
    td.append(cell);
    return td;
  }

  function statusTone(status) {
    const upper = String(status || "").toUpperCase();
    if (upper.startsWith("MISMATCH") || upper.startsWith("ERROR")) {
      return "tone-danger";
    }
    if (upper.startsWith("CAPTURE_FAILED")) return "tone-warn";
    if (upper.startsWith("NO_OSD")) return "tone-info";
    if (upper === "OK") return "tone-ok";
    return "tone-muted";
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Number(seconds) || 0);
    if (total >= 60) {
      const minutes = Math.floor(total / 60);
      const rest = Math.round(total % 60);
      return `${minutes} 分 ${rest} 秒`;
    }
    return `${Math.round(total)} 秒`;
  }

  function buildEmpty(data) {
    const card = element("div", "empty-card");
    const iconText = data.search ? "⌕" : "—";
    card.append(element("div", "empty-icon", iconText));

    let title;
    let description;
    if (data.search) {
      title = "没有匹配的通道编码";
      description =
        `没有找到通道编码包含“${data.search}”的当天记录。可以清除搜索查看全部结果。`;
    } else if (data.isToday) {
      title = "今天还没有异常记录";
      description =
        "osd_error.txt 目前为空，或所有通道检测正常。点击刷新可获取最新结果。";
    } else {
      title = "该日期没有归档记录";
      description = `Logs 中没有 ${data.date} 的归档异常记录，请选择其他日期。`;
    }

    card.append(element("h2", "", title));
    card.append(element("p", "", description));

    if (data.search) {
      const button = element("button", "btn ghost", "清除搜索");
      button.type = "button";
      button.dataset.action = "clear-search";
      card.append(button);
    }
    return card;
  }

  function renderError(error) {
    state.data = null;
    state.streaming = null;
    els.chartPanel.classList.add("hidden");
    if (state.chartModalOpen) closeChartModal();
    if (state.imageOpen) closeImageModal();
    els.results.replaceChildren();
    els.results.classList.remove("is-loading");

    const card = element("div", "empty-card error");
    card.append(element("div", "empty-icon", "!"));
    card.append(element("h2", "", "读取失败"));
    card.append(element("p", "", error && error.message ? error.message : "无法读取检测结果。"));

    const button = element("button", "btn primary", "重试");
    button.type = "button";
    button.dataset.action = "retry";
    card.append(button);
    els.results.append(card);

    setText(els.statTotal, "—");
    setText(els.statRounds, "—");
    setText(els.statFilter, "—");
    setText(els.statSource, "—");
    els.statTotalFoot.textContent = "—";
    els.statRoundsFoot.textContent = "—";
    els.statFilterFoot.textContent = "—";
    els.statSourceFoot.textContent = "—";
    els.notice.classList.add("hidden");
    els.pageSummary.textContent = "—";
    els.pager.replaceChildren();
  }

  function renderPagination(data) {
    const total = Number(data.totalMatched) || 0;
    const totalPages = Number(data.totalPages) || 0;

    if (total === 0) {
      els.pageSummary.textContent = "共 0 条记录";
    } else {
      els.pageSummary.textContent =
        `共 ${formatNumber(total)} 条 · 第 ${data.page} / ${totalPages} 页`;
    }

    els.pager.replaceChildren();
    if (totalPages <= 0) return;

    els.pager.append(pagerButton("«", 1, data.page <= 1, "第一页"));
    els.pager.append(pagerButton("‹", data.page - 1, data.page <= 1, "上一页"));

    for (const item of pageItems(data.page, totalPages)) {
      if (item === "…") {
        els.pager.append(element("span", "page-ellipsis", "…"));
        continue;
      }
      const button = element("button", "page-btn", String(item));
      button.type = "button";
      button.dataset.page = String(item);
      if (item === data.page) {
        button.classList.add("active");
        button.setAttribute("aria-current", "page");
      }
      button.setAttribute("aria-label", `第 ${item} 页`);
      els.pager.append(button);
    }

    els.pager.append(pagerButton("›", data.page + 1, data.page >= totalPages, "下一页"));
    els.pager.append(pagerButton("»", totalPages, data.page >= totalPages, "最后一页"));
  }

  function pagerButton(label, page, disabled, ariaLabel) {
    const button = element("button", "page-btn", label);
    button.type = "button";
    button.dataset.page = String(page);
    button.disabled = Boolean(disabled);
    button.setAttribute("aria-label", ariaLabel);
    return button;
  }

  function pageItems(current, total) {
    if (total <= 7) {
      return Array.from({ length: total }, (_, index) => index + 1);
    }

    const items = [1];
    const start = Math.max(2, current - 1);
    const end = Math.min(total - 1, current + 1);

    if (start > 2) items.push("…");
    for (let page = start; page <= end; page += 1) items.push(page);
    if (end < total - 1) items.push("…");
    items.push(total);
    return items;
  }

  function syncUrl(data) {
    const params = new URLSearchParams();
    if (!data.isToday) params.set("date", data.date);
    if (data.page > 1) params.set("page", String(data.page));
    if (data.search) params.set("q", data.search);
    const query = params.toString();
    const url = `${window.location.pathname}${query ? `?${query}` : ""}`;
    window.history.replaceState(null, "", url);
  }

  function scrollToResults() {
    const top = els.results.getBoundingClientRect().top + window.scrollY - 16;
    window.scrollTo({ top, behavior: "smooth" });
  }

  init();
})();
