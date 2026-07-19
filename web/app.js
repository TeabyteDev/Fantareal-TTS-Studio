(() => {
  "use strict";

  const host = window.fantarealExtension;
  const state = {
    settings: null,
    assets: { gpt: [], sovits: [], audio: [] },
    history: [],
    runtime: null,
    install: null,
    runtimePollTimer: null
  };
  const $ = (selector) => document.querySelector(selector);
  const elements = {
    hostStatus: $("#hostStatus"),
    message: $("#message"),
    apiUrl: $("#apiUrl"),
    activeVoice: $("#activeVoice"),
    healthResult: $("#healthResult"),
    runtimeDevice: $("#runtimeDevice"),
    runtimeStatus: $("#runtimeStatus"),
    runtimeProgress: $("#runtimeProgress"),
    runtimeInstallStatus: $("#runtimeInstallStatus"),
    runtimeLog: $("#runtimeLog"),
    installRuntimeButton: $("#installRuntimeButton"),
    cancelRuntimeButton: $("#cancelRuntimeButton"),
    launchRuntimeButton: $("#launchRuntimeButton"),
    stopRuntimeButton: $("#stopRuntimeButton"),
    previewButton: $("#previewButton"),
    previewText: $("#previewText"),
    previewAudio: $("#previewAudio"),
    voiceList: $("#voiceList"),
    historyList: $("#historyList")
  };

  function showMessage(message, error = false) {
    elements.message.textContent = message || "";
    elements.message.hidden = !message;
    elements.message.classList.toggle("error", error);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  async function invoke(method, params = {}) {
    if (!host) throw new Error("当前页面没有连接 Fantareal Extension Host");
    return host.invoke(method, params);
  }

  function optionList(values, selected, placeholder = "未选择") {
    const options = [`<option value="">${placeholder}</option>`];
    for (const value of values || []) {
      const escaped = escapeHtml(value);
      options.push(`<option value="${escaped}" ${value === selected ? "selected" : ""}>${escaped}</option>`);
    }
    return options.join("");
  }

  function renderVoices() {
    const voices = state.settings?.voices || [];
    elements.activeVoice.innerHTML = voices
      .map((voice) => `<option value="${escapeHtml(voice.id)}">${escapeHtml(voice.name)}</option>`)
      .join("");
    elements.activeVoice.value = state.settings?.activeVoiceId || voices[0]?.id || "";
    elements.voiceList.replaceChildren();
    if (!voices.length) {
      elements.voiceList.innerHTML = '<div class="empty">还没有声线。</div>';
      return;
    }
    voices.forEach((voice, index) => {
      const card = document.createElement("article");
      card.className = "voiceCard";
      card.innerHTML = `
        <div class="voiceCardHeader"><strong>声线 ${index + 1}</strong><button data-remove-voice="${index}" type="button">删除</button></div>
        <div class="voiceGrid">
          <div><label>Voice ID</label><input data-voice-field="id" value="${escapeHtml(voice.id)}"></div>
          <div><label>显示名称</label><input data-voice-field="name" value="${escapeHtml(voice.name)}"></div>
          <div><label>GPT 权重</label><select data-voice-field="gptWeights">${optionList(state.assets.gpt, voice.gptWeights)}</select></div>
          <div><label>SoVITS 权重</label><select data-voice-field="sovitsWeights">${optionList(state.assets.sovits, voice.sovitsWeights)}</select></div>
          <div><label>参考音频</label><select data-voice-field="referenceAudio">${optionList(state.assets.audio, voice.referenceAudio)}</select></div>
          <div><label>Locale</label><input data-voice-field="locale" value="${escapeHtml(voice.locale || "zh-CN")}"></div>
        </div>
        <label>参考文本</label><textarea data-voice-field="promptText">${escapeHtml(voice.promptText)}</textarea>
      `;
      card.querySelectorAll("[data-voice-field]").forEach((control) => {
        control.addEventListener("input", () => { voice[control.dataset.voiceField] = control.value; });
      });
      card.querySelector("[data-remove-voice]").addEventListener("click", () => {
        state.settings.voices.splice(index, 1);
        renderVoices();
      });
      elements.voiceList.append(card);
    });
  }

  function renderHistory() {
    elements.historyList.replaceChildren();
    if (!state.history.length) {
      elements.historyList.innerHTML = '<div class="empty">暂无生成历史。</div>';
      return;
    }
    state.history.forEach((item) => {
      const card = document.createElement("article");
      card.className = "historyCard";
      card.innerHTML = `<strong>${escapeHtml(item.voiceName || item.voiceId || "voice")}</strong><p>${escapeHtml(item.textPreview)}</p><p>${escapeHtml(item.createdAt)} · ${Number(item.size) || 0} bytes</p>`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "删除缓存与记录";
      remove.addEventListener("click", async () => {
        const result = await invoke("ttsStudio.deleteHistory", { audioId: item.id });
        state.history = result.items || [];
        renderHistory();
      });
      card.append(remove);
      elements.historyList.append(card);
    });
  }

  function renderRuntime(runtime) {
    const probe = runtime?.probe || {};
    const installed = runtime?.installed;
    elements.runtimeStatus.textContent = [
      installed ? `${installed.version || "GPT-SoVITS"} · ${String(installed.commit || "").slice(0, 10)}` : "尚未安装 runtime",
      runtime?.running ? `runtime 运行中 (PID ${runtime.pid})` : "runtime 未由插件启动",
      probe.available ? "GPT-SoVITS API 可用" : (probe.message || "API 不可用")
    ].join(" · ");
    elements.launchRuntimeButton.disabled = !installed || Boolean(runtime?.running) || Boolean(probe.available);
    elements.stopRuntimeButton.disabled = !runtime?.running;
  }

  function renderInstall(install) {
    const running = Boolean(install?.running);
    const progress = Number(install?.progress) || 0;
    const percent = Math.round(progress * 100);
    const status = install?.status || "idle";
    const step = install?.step || status;
    elements.runtimeProgress.value = progress;
    elements.runtimeInstallStatus.textContent = `${status} · ${step} · ${percent}%${install?.error ? ` · ${install.error}` : ""}`;
    const logs = [];
    if (install?.logTail) logs.push(`[INSTALL]\n${install.logTail}`);
    if (state.runtime?.logTail) logs.push(`[RUNTIME]\n${state.runtime.logTail}`);
    elements.runtimeLog.textContent = logs.join("\n\n") || "暂无日志";
    elements.installRuntimeButton.disabled = running;
    elements.installRuntimeButton.textContent = install?.installed ? "修复 / 重新安装" : "安装 runtime";
    elements.cancelRuntimeButton.disabled = !running;
    elements.runtimeDevice.disabled = running;
  }

  function scheduleRuntimePoll() {
    if (state.runtimePollTimer) window.clearTimeout(state.runtimePollTimer);
    state.runtimePollTimer = null;
    const waitingForRuntime = state.runtime?.running && !state.runtime?.probe?.available;
    if (!state.install?.running && !waitingForRuntime) return;
    state.runtimePollTimer = window.setTimeout(() => {
      refreshRuntime().catch((error) => showMessage(error.message, true));
    }, 1000);
  }

  async function refreshRuntime() {
    const [runtime, install] = await Promise.all([
      invoke("ttsStudio.runtimeStatus"),
      invoke("ttsStudio.runtimeInstallStatus")
    ]);
    state.runtime = runtime;
    state.install = install;
    renderRuntime(runtime);
    renderInstall(install);
    scheduleRuntimePoll();
  }

  async function loadState() {
    showMessage("");
    const result = await invoke("ttsStudio.getState");
    state.settings = result.settings;
    state.assets = result.assets || state.assets;
    state.history = result.history || [];
    state.runtime = result.runtime;
    elements.apiUrl.value = state.settings.apiUrl;
    elements.runtimeDevice.value = state.settings.runtimeDevice || "cpu";
    renderVoices();
    renderHistory();
    renderRuntime(result.runtime);
    await refreshRuntime();
  }

  async function saveSettings() {
    state.settings.apiUrl = elements.apiUrl.value;
    state.settings.activeVoiceId = elements.activeVoice.value;
    state.settings.runtimeDevice = elements.runtimeDevice.value;
    const result = await invoke("ttsStudio.saveSettings", { settings: state.settings });
    state.settings = result.settings;
    renderVoices();
    showMessage("设置已保存。");
  }

  async function importAsset(kind) {
    const accept = kind === "gpt" ? [".ckpt"] : kind === "sovits" ? [".pth", ".pt"] : ["audio/*", ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"];
    const file = await host.pickInput({ accept });
    if (!file) return;
    const result = await invoke("ttsStudio.importAsset", { kind, path: file.path, name: file.name });
    state.assets = result.assets || state.assets;
    renderVoices();
    showMessage(`已导入 ${result.item?.name || file.name}`);
  }

  $("#saveButton").addEventListener("click", () => saveSettings().catch((error) => showMessage(error.message, true)));
  $("#reloadButton").addEventListener("click", () => loadState().catch((error) => showMessage(error.message, true)));
  $("#healthButton").addEventListener("click", async () => {
    try {
      const result = await invoke("tts.health", { providerId: "gpt-sovits" });
      elements.healthResult.textContent = `${result.available ? "可用" : "不可用"} · ${result.message || ""}`;
    } catch (error) { showMessage(error.message, true); }
  });
  $("#runtimeButton").addEventListener("click", async () => {
    try { await refreshRuntime(); } catch (error) { showMessage(error.message, true); }
  });
  elements.installRuntimeButton.addEventListener("click", async () => {
    try {
      showMessage("runtime 安装已开始，可以留在此页查看进度，也可以取消。请勿强制关闭主程序。");
      state.install = await invoke("ttsStudio.runtimeInstall", { device: elements.runtimeDevice.value });
      renderInstall(state.install);
      scheduleRuntimePoll();
    } catch (error) { showMessage(error.message, true); }
  });
  elements.cancelRuntimeButton.addEventListener("click", async () => {
    try {
      state.install = await invoke("ttsStudio.runtimeCancel");
      renderInstall(state.install);
      showMessage("runtime 安装已取消，原有可用版本保持不变。");
    } catch (error) { showMessage(error.message, true); }
  });
  elements.launchRuntimeButton.addEventListener("click", async () => {
    try {
      state.runtime = await invoke("ttsStudio.runtimeLaunch");
      renderRuntime(state.runtime);
      scheduleRuntimePoll();
      showMessage("GPT-SoVITS API 已启动，首次加载模型可能需要一些时间。");
    } catch (error) { showMessage(error.message, true); }
  });
  elements.stopRuntimeButton.addEventListener("click", async () => {
    try {
      state.runtime = await invoke("ttsStudio.runtimeStop");
      renderRuntime(state.runtime);
      scheduleRuntimePoll();
      showMessage("GPT-SoVITS API 已停止。");
    } catch (error) { showMessage(error.message, true); }
  });
  elements.previewButton.addEventListener("click", async () => {
    const text = elements.previewText.value.trim();
    if (!text) {
      showMessage("请先输入试听文本。", true);
      return;
    }
    elements.previewButton.disabled = true;
    try {
      const result = await invoke("ttsStudio.preview", {
        voiceId: elements.activeVoice.value,
        requestId: `preview-${Date.now()}`,
        text
      });
      const audio = result.audio;
      elements.previewAudio.src = `data:${audio.mediaType};base64,${audio.base64}`;
      elements.previewAudio.hidden = false;
      await elements.previewAudio.play().catch(() => {});
      showMessage(`试听已生成 · ${audio.size} bytes`);
      const history = await invoke("ttsStudio.history");
      state.history = history.items || [];
      renderHistory();
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      elements.previewButton.disabled = false;
    }
  });
  $("#discoverButton").addEventListener("click", async () => {
    try {
      const result = await invoke("ttsStudio.discover");
      state.assets = result.assets || state.assets;
      renderVoices();
      showMessage("已重新扫描 assets 声线库。");
    } catch (error) { showMessage(error.message, true); }
  });
  $("#addVoiceButton").addEventListener("click", () => {
    state.settings.voices.push({ id: `voice-${state.settings.voices.length + 1}`, name: "新声线", locale: "zh-CN", gptWeights: "", sovitsWeights: "", referenceAudio: "", promptText: "", promptLanguage: "zh", textLanguage: "zh" });
    renderVoices();
  });
  document.querySelectorAll("[data-import-kind]").forEach((button) => {
    button.addEventListener("click", () => importAsset(button.dataset.importKind).catch((error) => showMessage(error.message, true)));
  });

  if (host) {
    elements.hostStatus.textContent = "已连接宿主";
    elements.hostStatus.classList.add("ready");
    loadState().catch((error) => showMessage(error.message, true));
  } else {
    elements.hostStatus.textContent = "仅预览模式";
    showMessage("此页面需要从 Fantareal 插件中心打开。", true);
  }
})();
