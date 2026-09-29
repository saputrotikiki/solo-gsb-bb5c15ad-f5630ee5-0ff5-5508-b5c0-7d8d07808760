/* 科研配方量纲校验台 —— 前端逻辑（原生 JS，无依赖） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const exprEl = $("#expr");
const targetEl = $("#target");
const varsEl = $("#vars");
const resultEl = $("#result");

let lastExport = null;

/* ---------------- 工具 ---------------- */

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmt(x) {
  if (typeof x !== "number") return String(x);
  if (x === 0) return "0";
  if (!isFinite(x)) return String(x);
  const a = Math.abs(x);
  if (a >= 1e12 || a < 1e-9) return x.toExponential(6);
  return String(Number(x.toPrecision(12)));
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return res.json();
}

function download(filename, text) {
  const blob = new Blob([text], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/* ---------------- 变量编辑 ---------------- */

function addVarRow(name = "", expr = "", uncert = "") {
  const row = document.createElement("div");
  row.className = "var-row";
  row.innerHTML =
    `<input class="mono vname" placeholder="名称" value="${escapeHtml(name)}" spellcheck="false">` +
    `<span class="eq">=</span>` +
    `<input class="mono vexpr" placeholder="取值表达式，如 3 m/s" value="${escapeHtml(expr)}" spellcheck="false">` +
    `<span class="eq ulabel" title="标准不确定度（带与变量一致的单位）">u</span>` +
    `<input class="mono vuncert" placeholder="标准不确定度，如 0.1 m/s（可选）" value="${escapeHtml(uncert)}" spellcheck="false">` +
    `<button class="btn small danger vdel" title="删除该变量">✕</button>`;
  row.querySelector(".vdel").addEventListener("click", () => row.remove());
  varsEl.appendChild(row);
}

function collectVariables() {
  const vars = {};
  varsEl.querySelectorAll(".var-row").forEach((row) => {
    const name = row.querySelector(".vname").value.trim();
    const expr = row.querySelector(".vexpr").value;
    if (name) vars[name] = expr;
  });
  return vars;
}

function collectUncertainties() {
  const uncert = {};
  varsEl.querySelectorAll(".var-row").forEach((row) => {
    const name = row.querySelector(".vname").value.trim();
    const val = row.querySelector(".vuncert").value.trim();
    if (name && val) uncert[name] = val;
  });
  return uncert;
}

function markUncertaintyInputs(errorSources) {
  varsEl.querySelectorAll(".var-row").forEach((row) => {
    const name = row.querySelector(".vname").value.trim();
    row.querySelector(".vuncert").classList.toggle(
      "input-bad", Boolean(name) && errorSources.has(`uncert:${name}`));
  });
}

/* ---------------- 校验与渲染 ---------------- */

async function runCheck() {
  markUncertaintyInputs(new Set());  // 清除上一次的输入框标红
  const body = {
    expression: exprEl.value,
    variables: collectVariables(),
    target_unit: targetEl.value || null,
    uncertainties: collectUncertainties(),
  };
  let res;
  try {
    res = await api("/api/check", { method: "POST", body: JSON.stringify(body) });
  } catch (err) {
    resultEl.innerHTML = `<div class="status bad">请求失败：${escapeHtml(err.message)}</div>`;
    return;
  }
  lastExport = { app: "dimcheck", kind: "check-result", request: body, response: res };
  $("#exportResult").disabled = false;
  renderResult(res, body);
}

function sourceOf(err, body) {
  if (err.source === "expression") return ["表达式", body.expression];
  if (err.source === "target") return ["目标单位", body.target_unit || ""];
  if (err.source && err.source.startsWith("var:")) {
    const name = err.source.slice(4);
    return [`变量 ${name}`, (body.variables || {})[name] ?? ""];
  }
  if (err.source && err.source.startsWith("uncert:")) {
    const name = err.source.slice(7);
    return [`不确定度 ${name}`, (body.uncertainties || {})[name] ?? ""];
  }
  return [err.source, ""];
}

function fragmentHtml(text, start, end) {
  start = Math.max(0, Math.min(start, text.length));
  end = Math.max(start, Math.min(end, text.length));
  const frag = text.slice(start, end);
  return (
    escapeHtml(text.slice(0, start)) +
    `<mark>${escapeHtml(frag) || "▸"}</mark>` +
    escapeHtml(text.slice(end))
  );
}

function renderResult(res, body) {
  const parts = [];
  const errors = res.errors || [];

  if (errors.length === 0 && res.result) {
    parts.push(`<div class="status ok">✓ 校验通过，量纲一致</div>`);
  } else if (errors.length > 0) {
    parts.push(`<div class="status bad">✗ 发现 ${errors.length} 个错误</div>`);
  }

  for (const w of res.warnings || []) {
    parts.push(`<div class="status warn">⚠ ${escapeHtml(w.message)}</div>`);
  }

  markUncertaintyInputs(new Set(errors.map((e) => e.source)));

  for (const err of errors) {
    const [label, text] = sourceOf(err, body);
    const frag = text.slice(err.start, err.end);
    const perturbNote = err.perturbed
      ? `<div class="muted small-text">扰动来源：变量 <code>${escapeHtml(err.perturbed)}</code>` +
        `（${err.direction === "+" ? "正" : "负"}向扰动步长重算）</div>`
      : "";
    parts.push(`
      <div class="error-item">
        <div class="error-head">
          <span class="tag">${escapeHtml(label)}</span>
          <span class="kind">${escapeHtml(err.kind)}</span>
          <span>${escapeHtml(err.message)}</span>
        </div>
        <div class="code">${fragmentHtml(text, err.start, err.end)}</div>
        ${frag ? `<div class="muted small-text">出错片段：<code>${escapeHtml(frag)}</code>（位置 ${err.start}–${err.end}）</div>` : ""}
        ${perturbNote}
      </div>`);
  }

  if (res.result) {
    const r = res.result;
    const vec = r.dimension
      .map((e, i) => (e !== "0" ? `${r.dimension_names[i]} = ${e}` : null))
      .filter(Boolean)
      .join("，") || "全部为零（无量纲）";
    const derived = r.derived.length
      ? r.derived.map((d) => `<span class="chip">${escapeHtml(d)}</span>`).join(" ")
      : '<span class="muted">无</span>';

    const uc = r.uncertainty;
    const hasUc = Boolean(uc);
    const spanAttr = hasUc ? "" : ' colspan="2"';
    const headNominal = hasUc
      ? `<tr><th></th><th>标称值</th><th>标准不确定度 u</th></tr>` : "";
    const siUcCell = hasUc
      ? `<td class="mono uc">± ${fmt(uc.si_value)} ${escapeHtml(uc.si_unit)}</td>` : "";
    const targetUcCell = hasUc
      ? `<td class="mono uc">${uc.target ? `± ${fmt(uc.target.value)} ${escapeHtml(uc.target.unit)}` : '<span class="muted">—</span>'}</td>`
      : "";
    const targetRow = r.target
      ? `<tr><th>换算结果</th><td class="mono strong">${fmt(r.target.value)} ${escapeHtml(r.target.unit)}</td>${targetUcCell}</tr>`
      : "";

    let ucSection = "";
    if (hasUc) {
      const ucStatus = uc.sources.length
        ? `<div class="status uc-ok">∑ 合成标准不确定度（一阶 RSS，中心差分，相对步长 ${fmt(uc.step_relative)}）</div>`
        : `<div class="status uc-ok">已填写的标准不确定度均为 0，合成标准不确定度为 0</div>`;
      let srcRows = "";
      const totalSq = uc.sources.reduce((a, s) => a + s.contribution_si ** 2, 0);
      for (const s of uc.sources) {
        const pct = totalSq > 0 ? 100 * (s.contribution_si ** 2) / totalSq : 0;
        srcRows +=
          `<tr>` +
          `<td class="mono">${escapeHtml(s.name)}</td>` +
          `<td class="mono">± ${fmt(s.u_si)}</td>` +
          `<td class="mono muted">${fmt(s.sensitivity)}</td>` +
          `<td class="mono">± ${fmt(s.contribution_si)}</td>` +
          `<td class="mono muted">${fmt(pct)}%</td>` +
          `<td class="mono muted">${fmt(s.step_si)}</td>` +
          `</tr>`;
      }
      ucSection = `
        ${ucStatus}
        ${uc.sources.length ? `
        <details class="uc-details">
          <summary>各独立输入贡献（${uc.sources.length} 个扰动源）</summary>
          <table class="uc-table">
            <thead><tr>
              <th>输入</th><th>u（SI 单位）</th><th>灵敏度 ∂f/∂x</th>
              <th>贡献 |∂f/∂x|·u</th><th>方差占比</th><th>实际步长 h（SI）</th>
            </tr></thead>
            <tbody>${srcRows}</tbody>
          </table>
        </details>` : ""}
        <p class="muted small-text">
          合成方法：u<sub>c</sub>(y) = √(Σ(∂f/∂x<sub>i</sub>·u(x<sub>i</sub>))²)，
          各输入视为相互独立；灵敏度由双侧中心差分
          (f(x+h)−f(x−h))/(2h) 数值估计，对完整变量链与主表达式整体重算。
        </p>`;
    } else if (errors.some((e) => e.source && e.source.startsWith("uncert:")) ||
               errors.some((e) => e.perturbed)) {
      ucSection = `<div class="status bad">测量不确定度分析未完成：见上方错误，未给出合成不确定度。</div>`;
    } else {
      ucSection = `<p class="muted small-text">未填写标准不确定度：在变量行的「u」列填写带单位的标准不确定度即可启用 RSS 合成分析。</p>`;
    }

    parts.push(`
      <table class="result-table${hasUc ? " has-uc" : ""}">
        ${headNominal}
        <tr><th>量纲</th><td ${spanAttr} class="mono strong">${escapeHtml(r.dimension_str)}</td>${hasUc ? "<td></td>" : ""}</tr>
        <tr><th>量纲向量</th><td ${spanAttr} class="mono">${escapeHtml(vec)}</td>${hasUc ? "<td></td>" : ""}</tr>
        <tr><th>SI 数值</th><td class="mono strong">${fmt(r.si_value)} ${escapeHtml(r.si_unit)}</td>${siUcCell}</tr>
        <tr><th>等价单位</th><td ${spanAttr}>${derived}</td>${hasUc ? "<td></td>" : ""}</tr>
        ${targetRow}
      </table>
      <div class="uc-section">${ucSection}</div>`);
  }

  resultEl.innerHTML = parts.join("");
}

/* ---------------- 案例管理 ---------------- */

async function refreshCases() {
  const ul = $("#caseList");
  try {
    const { cases } = await api("/api/cases");
    ul.innerHTML = "";
    if (!cases.length) {
      ul.innerHTML = `<li class="muted">暂无保存的案例</li>`;
      return;
    }
    for (const c of cases) {
      const li = document.createElement("li");
      li.innerHTML =
        `<div class="case-meta"><strong>${escapeHtml(c.name)}</strong>` +
        `<span class="muted">${escapeHtml(c.created_at)}</span></div>` +
        `<div class="case-ops">` +
        `<button class="btn small" data-act="load">加载</button>` +
        `<button class="btn small danger" data-act="del">删除</button></div>`;
      li.querySelector('[data-act="load"]').addEventListener("click", () => loadCase(c.id));
      li.querySelector('[data-act="del"]').addEventListener("click", async () => {
        if (confirm(`删除案例「${c.name}」？`)) {
          await api(`/api/cases/${c.id}`, { method: "DELETE" });
          refreshCases();
        }
      });
      ul.appendChild(li);
    }
  } catch (err) {
    ul.innerHTML = `<li class="muted">加载失败：${escapeHtml(err.message)}</li>`;
  }
}

async function loadCase(id) {
  const c = await api(`/api/cases/${id}`);
  exprEl.value = c.expression;
  targetEl.value = c.target_unit || "";
  $("#caseName").value = c.name;
  varsEl.innerHTML = "";
  const vars = c.variables || {};
  const uncert = c.uncertainties || {};
  const names = Object.keys(vars);
  if (names.length === 0) addVarRow();
  else names.forEach((n) => addVarRow(n, vars[n], uncert[n] || ""));
  runCheck();
}

async function saveCase() {
  const name =
    $("#caseName").value.trim() || exprEl.value.trim().slice(0, 30) || "未命名案例";
  try {
    await api("/api/cases", {
      method: "POST",
      body: JSON.stringify({
        name,
        expression: exprEl.value,
        variables: collectVariables(),
        uncertainties: collectUncertainties(),
        target_unit: targetEl.value,
      }),
    });
    $("#caseName").value = name;
    refreshCases();
  } catch (err) {
    alert(`保存失败：${err.message}`);
  }
}

/* ---------------- 单位帮助 ---------------- */

async function loadUnits() {
  const box = $("#unitsHelp");
  try {
    const data = await api("/api/units");
    const groups = {};
    for (const u of data.units) (groups[u.category] ??= []).push(u);
    const parts = [];
    for (const [cat, list] of Object.entries(groups)) {
      const rows = list
        .map((u) => {
          const factor = u.factor !== 1 ? `<td class="muted mono">×${fmt(u.factor)}</td>` : "<td></td>";
          return `<tr><td class="mono">${escapeHtml(u.name)}</td><td>${escapeHtml(u.description)}</td>${factor}</tr>`;
        })
        .join("");
      parts.push(`<details><summary>${escapeHtml(cat)}（${list.length}）</summary><table>${rows}</table></details>`);
    }
    const constRows = data.constants
      .map((c) => `<tr><td class="mono">${escapeHtml(c.name)}</td><td>${escapeHtml(c.description)}</td></tr>`)
      .join("");
    parts.push(`<details><summary>内置常量（${data.constants.length}）</summary><table>${constRows}</table></details>`);
    const fnRows = data.functions
      .map((f) => `<tr><td class="mono">${escapeHtml(f.name)}(x)</td><td>${escapeHtml(f.description)}</td></tr>`)
      .join("");
    parts.push(`<details open><summary>函数（${data.functions.length}）</summary><table>${fnRows}</table></details>`);
    box.innerHTML = parts.join("");
  } catch (err) {
    box.innerHTML = `<p class="muted">加载失败：${escapeHtml(err.message)}</p>`;
  }
}

/* ---------------- 事件绑定 ---------------- */

$("#check").addEventListener("click", runCheck);
$("#addVar").addEventListener("click", () => addVarRow());
$("#saveCase").addEventListener("click", saveCase);
$("#exportAll").addEventListener("click", async () => {
  const res = await fetch("/api/export");
  download("dimcheck_cases.json", await res.text());
});
$("#exportResult").addEventListener("click", () => {
  if (lastExport) download("dimcheck_result.json", JSON.stringify(lastExport, null, 2));
});
$("#sample").addEventListener("click", () => {
  exprEl.value = "0.5*mass*v^2 + mass*g0*h0";
  targetEl.value = "kJ";
  varsEl.innerHTML = "";
  addVarRow("mass", "2 kg", "0.01 kg");
  addVarRow("v", "10 m/s", "0.1 m/s");
  addVarRow("h0", "5 m", "0.02 m");
  $("#caseName").value = "动能 + 重力势能";
  runCheck();
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") runCheck();
});

addVarRow();
refreshCases();
loadUnits();
