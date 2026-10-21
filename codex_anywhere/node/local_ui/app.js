(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const form = $("pair-form");
  const fields = {
    server: $("server"),
    code: $("pair-code"),
    name: $("device-name"),
    codex_bin: $("codex-bin"),
    allow_insecure: $("allow-insecure"),
  };
  const states = {
    unpaired: ["尚未配对", "填写管理中心地址和配对码，连接这台设备。"],
    stopped: ["已停止", "配对信息已保存，可以启动节点连接管理中心。"],
    pairing: ["正在配对", "正在验证配对信息，成功后会自动启动节点。"],
    starting: ["正在启动", "正在启动本机 Codex 并连接管理中心。"],
    connected: ["已连接", "节点正在运行，可以在管理中心查看和继续本机会话。"],
    reconnecting: [
      "正在重连",
      "与管理中心的连接已断开，节点正在尝试恢复连接。",
    ],
    stopping: ["正在停止", "正在结束本机节点运行，请稍候。"],
    error: ["连接异常", "请检查下方错误信息，修正设置后重试。"],
  };
  let status = null;
  let csrfToken = "";
  let initialized = false;
  let editingPairing = false;
  let confirmingStop = false;
  let mutationPending = false;
  let actionError = "";
  let transportError = "";
  let feedback = "";
  let statusRequest = null;
  let revision = 0;

  function text(id, value) {
    $(id).textContent = value || "—";
  }
  function busy() {
    return (
      mutationPending ||
      ["pairing", "starting", "stopping"].includes(status?.status)
    );
  }
  function populateFields(value) {
    fields.server.value = value.server || "";
    fields.name.value = value.name || "";
    fields.codex_bin.value = value.codex_bin || "codex";
    fields.allow_insecure.checked = value.allow_insecure === true;
  }
  function acceptStatus(value) {
    if (!value || typeof value !== "object")
      throw new Error("本地服务返回了无效的节点状态。");
    status = value;
    if (!initialized) {
      // Subsequent read-only polls never overwrite the user's configuration draft.
      populateFields(value);
      initialized = true;
    }
    if (value.running) editingPairing = false;
    if (!value.running) confirmingStop = false;
  }
  function render() {
    const state = states[status?.status] || [
      "正在读取",
      "正在读取本地节点状态…",
    ];
    text("status-label", state[0]);
    $("status-badge").dataset.state = status?.status || "loading";
    text("status-description", state[1]);
    $("connection-summary").hidden = !status?.paired;
    text("summary-server", status?.server);
    text("summary-name", status?.name);
    text("summary-platform", status?.platform);

    const error = actionError || transportError || status?.last_error || "";
    $("error-message").hidden = !error;
    $("error-message").textContent = error;
    $("feedback-message").hidden = !feedback;
    $("feedback-message").textContent = feedback;
    $("refresh-status").hidden = !transportError;
    $("refresh-status").disabled = mutationPending;

    $("node-actions").hidden = !status?.paired;
    $("start-node").disabled = !status || !!status.running || busy();
    text(
      "start-node",
      status?.status === "error" || status?.last_error
        ? "重试连接"
        : "启动节点",
    );
    $("request-stop").disabled = !status?.running || busy();
    $("edit-pairing").disabled = !status || !!status.running || busy();
    $("edit-pairing").title = status?.running
      ? "请先停止节点后修改配对"
      : "修改管理中心地址或配对信息";
    $("stop-confirmation").hidden = !confirmingStop;
    $("confirm-stop").disabled = !status?.running || busy();
    $("cancel-stop").disabled = mutationPending;

    const pairingForm = !!status && (!status.paired || editingPairing);
    $("configuration-card").hidden = !status;
    $("pair-fields").hidden = !pairingForm;
    $("pair-fields").disabled = busy() || !!status?.running;
    // Hidden pairing fields must not block the separate Start action.
    fields.server.required = pairingForm;
    fields.code.required = pairingForm;
    fields.name.required = pairingForm;
    $("pair-actions").hidden = !pairingForm;
    $("replace-confirmation").hidden = !editingPairing || !status?.paired;
    $("replace-pairing").disabled = busy() || !!status?.running;
    $("cancel-pairing").hidden = !editingPairing;
    $("cancel-pairing").disabled = mutationPending;
    $("pair-submit").disabled =
      !status ||
      busy() ||
      !!status.running ||
      (status.paired && !$("replace-pairing").checked);
    $("pair-submit").textContent =
      busy() && status?.status === "pairing"
        ? "正在配对…"
        : editingPairing
          ? "替换配对并启动"
          : "配对并启动 →";
    fields.codex_bin.disabled = mutationPending;
    fields.allow_insecure.disabled = mutationPending;
    text(
      "configuration-heading",
      editingPairing
        ? "修改本机配对"
        : status?.paired
          ? "启动设置"
          : "连接管理中心",
    );
    text(
      "configuration-description",
      editingPairing
        ? "请先确认替换配对。现有 Codex 会话和本机节点状态资料会保留。"
        : status?.paired
          ? "高级设置会在下次启动节点时应用。"
          : "在管理中心创建配对码，将这台设备添加为节点。",
    );

    let hubUrl = "";
    try {
      const url = new URL(status?.server || "");
      if (
        ["https:", "http:"].includes(url.protocol) &&
        !url.username &&
        !url.password
      )
        hubUrl = url.href;
    } catch {
      /* No navigable server URL is available yet. */
    }
    $("open-hub").hidden = !status?.paired || !hubUrl;
    $("open-hub").href = hubUrl || "#";
  }

  async function request(path, payload) {
    let response;
    try {
      response = await fetch(path, {
        method: payload === undefined ? "GET" : "POST",
        credentials: "same-origin",
        cache: "no-store",
        ...(payload === undefined
          ? {}
          : {
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken,
              },
              body: JSON.stringify(payload),
            }),
      });
    } catch {
      throw new Error("无法连接本地节点服务。请确认本地服务仍在运行。");
    }
    const result = await response.json().catch(() => ({}));
    if (!response.ok)
      throw new Error(
        typeof result.error === "string"
          ? result.error
          : `请求失败（${response.status}）。`,
      );
    return result;
  }

  function loadStatus() {
    if (statusRequest) return statusRequest;
    if (mutationPending) return Promise.resolve();
    const version = revision;
    statusRequest = (async () => {
      try {
        const result = await request("/api/status");
        if (version !== revision) return;
        if (typeof result.csrf_token !== "string" || !result.csrf_token)
          throw new Error("未能获取本地服务的请求凭证，请重新读取状态。");
        csrfToken = result.csrf_token;
        acceptStatus(result.status);
        transportError = "";
      } catch (error) {
        if (version === revision) transportError = error.message;
      } finally {
        if (version === revision) render();
      }
    })().finally(() => {
      statusRequest = null;
    });
    return statusRequest;
  }

  async function mutate(path, payload) {
    if (mutationPending || !csrfToken) return false;
    mutationPending = true;
    revision += 1;
    actionError = "";
    feedback = "";
    render();
    let accepted = false;
    try {
      const result = await request(path, payload);
      acceptStatus(result.status);
      transportError = "";
      if (path === "/api/pair") {
        fields.code.value = "";
        $("replace-pairing").checked = false;
        editingPairing = false;
        feedback = "已提交配对，成功后节点会自动启动。";
      } else if (path === "/api/start")
        feedback = "已请求启动节点，正在更新连接状态。";
      else {
        confirmingStop = false;
        feedback = "已请求停止节点。";
      }
      accepted = true;
    } catch (error) {
      actionError = error.message;
    } finally {
      mutationPending = false;
      render();
    }
    return accepted;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (
      !status ||
      busy() ||
      status.running ||
      (status.paired && !editingPairing)
    )
      return;
    actionError = "";
    try {
      const server = fields.server.value.trim().replace(/\/+$/, "");
      const url = new URL(server);
      if (!["https:", "http:"].includes(url.protocol))
        throw new Error("管理中心地址需要使用 https:// 或 http://。");
      if (url.username || url.password)
        throw new Error("管理中心地址不能包含用户名或密码。");
      if (url.protocol === "http:" && !fields.allow_insecure.checked) {
        $("advanced-settings").open = true;
        throw new Error(
          "管理中心使用 HTTP。请在高级设置中明确开启“允许使用 HTTP 连接”。",
        );
      }
      const name = fields.name.value.trim();
      const code = fields.code.value.trim();
      if (!name || !code) throw new Error("请填写设备名称和配对码。");
      if (status.paired && !$("replace-pairing").checked)
        throw new Error("请先确认替换本机配对。");
      void mutate("/api/pair", {
        server,
        name,
        code,
        codex_bin: fields.codex_bin.value.trim() || "codex",
        allow_insecure: fields.allow_insecure.checked,
        ...(status.paired ? { replace_pairing: true } : {}),
      });
    } catch (error) {
      actionError =
        error instanceof TypeError
          ? "请填写有效的管理中心完整地址。"
          : error.message;
      render();
    }
  });
  $("start-node").addEventListener("click", () => {
    if (!status?.paired || status.running || busy()) return;
    void mutate("/api/start", {
      codex_bin: fields.codex_bin.value.trim() || "codex",
      allow_insecure: fields.allow_insecure.checked,
    });
  });
  $("request-stop").addEventListener("click", () => {
    if (!status?.running || busy()) return;
    confirmingStop = true;
    render();
    $("confirm-stop").focus();
  });
  $("cancel-stop").addEventListener("click", () => {
    confirmingStop = false;
    render();
  });
  $("confirm-stop").addEventListener("click", () => {
    if (status?.running && !busy()) void mutate("/api/stop", {});
  });
  $("edit-pairing").addEventListener("click", () => {
    if (!status?.paired || status.running || busy()) return;
    editingPairing = true;
    confirmingStop = false;
    $("replace-pairing").checked = false;
    actionError = "";
    feedback = "";
    render();
    fields.server.focus();
  });
  $("cancel-pairing").addEventListener("click", () => {
    editingPairing = false;
    fields.code.value = "";
    $("replace-pairing").checked = false;
    populateFields(status);
    actionError = "";
    render();
  });
  $("replace-pairing").addEventListener("change", render);
  $("refresh-status").addEventListener("click", () => void loadStatus());
  text("local-address", window.location.host);
  void loadStatus();
  window.setInterval(() => void loadStatus(), 2000);
})();
