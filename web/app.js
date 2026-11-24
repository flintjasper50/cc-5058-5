// 维修规划窗口前端：所有规划/执行均由后端真实运算，前端只做交互与呈现。
const $ = (id) => document.getElementById(id);
let meta = { scenarios: [], fault_types: [], tools: [], places: [] };
let snapshot = null;

async function api(path, body) {
  const opt = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
  const res = await fetch(path, opt);
  const data = await res.json();
  if (!res.ok) showError(data.error || "请求失败");
  else hideError();
  return data;
}

function showError(msg) {
  const box = $("errorBox");
  box.textContent = msg;
  box.classList.remove("hidden");
}
function hideError() { $("errorBox").classList.add("hidden"); }

function statusPill(s) {
  return `<span class="status-pill status-${s}">${s}</span>`;
}

async function loadMeta() {
  meta = await api("/api/scenarios");
  const sel = $("scenarioSelect");
  sel.innerHTML = meta.scenarios
    .map((s) => `<option value="${s.key}">${s.name}</option>`).join("");
  $("faultSelect").innerHTML = meta.fault_types
    .map((f) => `<option value="${f}">${f}</option>`).join("");
  renderToolRows(meta.scenarios[0].tool_at);
  sel.onchange = () => {
    const scn = meta.scenarios.find((s) => s.key === sel.value);
    $("faultSelect").value = scn.fault;
    $("boxTaken").checked = scn.box_taken_by_mate;
    $("donorValve").checked = scn.donor_valve_available;
    $("gasket").checked = scn.gasket_available;
    $("sealIntact").checked = scn.seal_intact;
    renderToolRows(scn.tool_at);
  };
}

function renderToolRows(toolAt) {
  $("toolRows").innerHTML = meta.tools.map((t) => {
    const place = toolAt[t];
    const opts = meta.places.map((p) =>
      `<option value="${p}" ${p === place ? "selected" : ""}>${p}</option>`).join("");
    return `<div class="tool-row">
      <span>${t}</span>
      <label class="chk"><input type="checkbox" data-remove="${t}" ${place ? "" : "checked"} /> 移除</label>
      <select data-tool="${t}" ${place ? "" : "disabled"}>${opts}</select>
    </div>`;
  }).join("");
  $("toolRows").querySelectorAll("input[data-remove]").forEach((cb) => {
    cb.onchange = () => {
      const sel = $("toolRows").querySelector(`select[data-tool="${cb.dataset.remove}"]`);
      sel.disabled = cb.checked;
    };
  });
}

function readCustom() {
  const toolAt = {};
  const removeTools = [];
  $("toolRows").querySelectorAll(".tool-row").forEach((row) => {
    const tool = row.querySelector("select").dataset.tool;
    const removed = row.querySelector("input").checked;
    if (removed) removeTools.push(tool);
    else toolAt[tool] = row.querySelector("select").value;
  });
  return {
    fault: $("faultSelect").value,
    box_taken_by_mate: $("boxTaken").checked,
    donor_valve_available: $("donorValve").checked,
    gasket_available: $("gasket").checked,
    seal_intact: $("sealIntact").checked,
    tool_at: toolAt,
    remove_tools: removeTools,
  };
}

async function createSession(usePreset) {
  const payload = usePreset
    ? { scenario: $("scenarioSelect").value }
    : { name: "自定义场景", ...readCustom() };
  snapshot = await api("/api/session", payload);
  render();
}

function renderPlan(snap) {
  const lp = snap.last_plan;
  if (!lp) { $("planMeta").textContent = "尚无规划"; $("planList").innerHTML = ""; return; }
  $("planMeta").innerHTML =
    `规划状态 ${statusPill(lp.status)} &nbsp; 真实代价 <b>${lp.cost}</b> &nbsp; ` +
    `扩展节点 <b>${lp.expanded}</b> &nbsp; 生成节点 <b>${lp.generated}</b><br>` +
    `<span>${lp.reason}</span>` +
    (lp.why_infeasible && lp.why_infeasible.length
      ? `<ul>${lp.why_infeasible.map((r) => `<li>${r}</li>`).join("")}</ul>` : "");
  $("planList").innerHTML = lp.steps.map((st) =>
    `<li class="${st.sense ? "sense" : ""}">
       ${st.action} <span class="cost">代价 ${st.cost}${st.sense ? " · 感知动作" : ""}</span>
       <span class="why">成立依据：${st.rationale}</span>
       ${st.pre && st.pre.length
         ? `<span class="why">前提读集（提交前在真世界复核）：${st.pre.join("、")}</span>`
         : ""}
     </li>`).join("") || "<li>（无动作）</li>";
}

function renderRunStatus(snap) {
  const state = snap.failed ? "失败/不可行" : (snap.finished ? "已完成" : "执行中");
  const cls = snap.failed ? "no" : (snap.finished ? "ok" : "");
  $("runState").innerHTML =
    `目标 <b>${snap.goal}</b> &nbsp; 运行状态 <span class="tag ${cls}">${state}</span> &nbsp; ` +
    `已结束 <b>${snap.finished}</b> &nbsp; 失败/不可行 <b>${snap.failed}</b> &nbsp; ` +
    `真世界版本 <b>${snap.world_version || 0}</b> &nbsp; 累计真实代价 <b>${snap.total_cost}</b>`;
  const anomalies = snap.anomalies || [];
  $("anomalyBox").className = "audit " +
    (anomalies.some((a) => a.includes("受阻") || a.includes("失败") || a.includes("异常") || a.includes("伪报") || a.includes("不可行"))
      ? "fail" : "pass");
  $("anomalyBox").innerHTML = "规划步骤与维修动作异常：" +
    anomalies.map((a) => `<div>· ${a}</div>`).join("");
}

function renderHistory(snap) {
  renderRunStatus(snap);
  $("history").innerHTML = snap.history.map((h) => {
    const cls = h.blocked ? "blocked" : (h.sense ? "sense" : "");
    const tag = h.blocked ? "受阻" : (h.sense ? "感知" : "执行");
    const anomalies = h.anomalies || [];
    return `<div class="step ${cls}">
      <div class="head">
        <span class="tag ${h.blocked ? "blocked" : h.sense ? "sense" : "exec"}">${tag}</span>
        <b>${h.index}. ${h.action}</b>
        <span class="tag ${h.confirmed ? "ok" : "no"}">${h.confirmed ? "效果已确认" : "效果未确认"}</span>
        <span class="tag ${h.replan_status ? `status-${h.replan_status}` : ""}">${h.replan_status || "-"}</span>
        <span>累计 ${h.cumulative_cost}</span>
        <span>真世界版本 ${h.world_version || 0}</span>
      </div>
      <div class="detail">
        ${h.blocking_reason ? `<div>受阻原因：${h.blocking_reason}</div>` : ""}
        ${h.decision ? `<div>规划决策：${h.decision}</div>` : ""}
        ${h.observation.map((o) => `<div>观察反馈：${o}</div>`).join("")}
        ${h.expected_effects.map((e) => `<div>${e}</div>`).join("")}
        ${h.fact_added && h.fact_added.length
          ? `<div>世界效果·新增事实：${h.fact_added.join("、")}</div>` : ""}
        ${h.fact_removed && h.fact_removed.length
          ? `<div>世界效果·移除事实：${h.fact_removed.join("、")}</div>` : ""}
        ${!h.audit_ok ? `<div style="color:var(--bad)">守恒异常：${h.audit_problems.join("；")}</div>` : ""}
        ${anomalies.length
          ? `<div class="anomaly">异常标注：${anomalies.map((a) => `· ${a}`).join(" ")}</div>` : ""}
      </div>
    </div>`;
  }).join("") || "<div class='meta'>尚未执行任何动作</div>";
}

function renderTables(snap) {
  const rows = (table) => Object.entries(table)
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([k, v]) => {
      const phantom = v.startsWith("ASSUMED");
      return `<tr class="${phantom ? "phantom" : ""}">
        <td>${k}</td><td class="${phantom ? "assumed" : ""}">${v}</td></tr>`;
    }).join("");
  $("worldTable").innerHTML =
    `<tr><th>对象</th><th>真世界位置/状态</th></tr>` + rows(snap.world_locations);
  $("beliefTable").innerHTML =
    `<tr><th>对象</th><th>信念位置/状态</th></tr>` + rows(snap.belief_locations);

  // 物品栏 chips：点击核对该物品在真世界/信念中的位置，识别幽灵零件
  const items = new Set([
    ...Object.keys(snap.world_locations), ...Object.keys(snap.belief_locations),
  ]);
  $("inventory").innerHTML = [...items].sort().map((it) => {
    const bv = snap.belief_locations[it];
    const assumed = bv && bv.startsWith("ASSUMED");
    return `<span class="chip ${assumed ? "assumed-chip" : ""}" data-item="${it}">
      ${it}${assumed ? "（假设）" : ""}</span>`;
  }).join("");
  $("inventory").querySelectorAll(".chip").forEach((c) => {
    c.onclick = () => showItemModal(snap, c.dataset.item);
  });

  const audit = $("auditBox");
  audit.className = "audit " + (snap.audit_ok ? "pass" : "fail");
  audit.innerHTML = (snap.audit_ok ? "资源守恒审计通过：" : "资源守恒审计失败：") +
    snap.audit.map((a) => `<div>· ${a}</div>`).join("");
}

function showItemModal(snap, item) {
  const wv = snap.world_locations[item] || "不存在于真世界";
  const bv = snap.belief_locations[item] || "不在信念中";
  const phantom = bv.startsWith("ASSUMED") ||
    (bv !== "不在信念中" && wv === "不存在于真世界");
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.innerHTML = `<div class="modal-card">
    <h3>物品核对：${item}</h3>
    <p class="kv">真世界：<b>${wv}</b></p>
    <p class="kv">角色信念：<b>${bv}</b></p>
    <p class="kv" style="color:${phantom ? "var(--bad)" : "var(--ok)"}">
      ${phantom
        ? "警告：信念中的该零件未被真世界证实，属于规划假设；规划回滚不得把它当成已用掉的实物。"
        : "一致：信念与真世界对该物品的描述不冲突。"}
    </p>
    <button id="closeModal">关闭</button>
  </div>`;
  document.body.appendChild(back);
  back.onclick = (e) => { if (e.target === back || e.target.id === "closeModal") back.remove(); };
}

function render() {
  if (!snapshot) return;
  renderPlan(snapshot);
  renderHistory(snapshot);
  renderTables(snapshot);
  const busy = snapshot.finished || snapshot.failed;
  $("stepBtn").disabled = busy;
  $("runBtn").disabled = busy;
}

function bind() {
  $("createBtn").onclick = () => createSession(true);
  $("stepBtn").onclick = async () => { snapshot = await api("/api/session/step", {}); render(); };
  $("runBtn").onclick = async () => { snapshot = await api("/api/session/run", { max_steps: 40 }); render(); };
  $("resetBtn").onclick = async () => { snapshot = await api("/api/session/reset", {}); render(); };
}

loadMeta().then(bind);
