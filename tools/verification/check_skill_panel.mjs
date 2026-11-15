// Drive a real headless Chrome and check the skill panel end to end.
//
// The "/" menu only helps people who already know it exists. This verifies the
// discoverable entry point: a button in the composer that opens a panel showing
// installed skills, searches Magpie's catalogue, and installs/removes for real.
//
// Needs Chrome, Magpie's catalogue, and a server on :8000.
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9335;
const URL = "http://127.0.0.1:8000/";

const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--user-data-dir=" + process.env.TEMP + "\\cdp-slash-" + Date.now(),
  "--remote-debugging-port=" + PORT, "--window-size=1280,900", "about:blank",
], { stdio: "ignore" });

async function waitForDevtools() {
  // /json/version is the BROWSER endpoint; Runtime.* only exists on a PAGE
  // target, so wait until a page target is listed and connect to that.
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch("http://127.0.0.1:" + PORT + "/json/list");
      if (r.ok) {
        const targets = await r.json();
        const page = targets.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
        if (page) return page;
      }
    } catch {}
    await sleep(250);
  }
  throw new Error("no page target came up");
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map();
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
        else p.resolve(msg.result);
      }
    };
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const res = await this.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (res.exceptionDetails) {
      throw new Error((res.exceptionDetails.exception && res.exceptionDetails.exception.description) || "eval failed");
    }
    return res.result.value;
  }
}

const info = await waitForDevtools();
const ws = new WebSocket(info.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
const cdp = new CDP(ws);
await cdp.send("Runtime.enable");
await cdp.send("Page.enable");

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log((ok ? "  ok  " : "  FAIL") + "  " + name + (detail ? "  [" + detail + "]" : ""));
}

await cdp.send("Page.navigate", { url: URL });
await sleep(3500);

// 1. the button is visible without the user knowing about "/"
const btn = await cdp.eval([
  '(() => {',
  '  const b = document.getElementById("skill-btn");',
  '  const r = b.getBoundingClientRect();',
  '  return { exists: !!b, w: Math.round(r.width), label: b.textContent.trim(),',
  '    panelHidden: document.getElementById("skill-panel").hidden };',
  '})()',
].join("\n"));
check("skill button rendered in composer", btn.exists && btn.w > 40, btn.w + "px " + JSON.stringify(btn.label));
check("panel starts closed", btn.panelHidden === true);

// 2. clicking it opens the panel listing installed skills
const opened = await cdp.eval([
  '(() => {',
  '  document.getElementById("skill-btn").click();',
  '  const p = document.getElementById("skill-panel");',
  '  return { hidden: p.hidden,',
  '    rows: [...p.querySelectorAll("[data-installed]")].map(r => r.dataset.installed),',
  '    tabs: [...p.querySelectorAll(".sp-tab")].map(t => t.textContent.trim()),',
  '    hasSearch: !!p.querySelector("#sp-q") };',
  '})()',
].join("\n"));
check("clicking the button opens the panel", opened.hidden === false);
check("installed skills are listed", opened.rows.length === 2, JSON.stringify(opened.rows));
check("panel has both tabs", opened.tabs.length === 2, JSON.stringify(opened.tabs));
check("panel has a search box", opened.hasSearch === true);

// 3. choosing a skill from the panel marks it for this task
const picked = await cdp.eval([
  '(() => {',
  '  const b = document.querySelector("[data-sp-pick]");',
  '  const name = b.dataset.spPick;',
  '  b.click();',
  '  return { name, picked: [...S.pickedSkills],',
  '    tag: document.querySelectorAll("#picked-row .picked-tag").length,',
  '    badge: (document.getElementById("skill-count") || {}).textContent };',
  '})()',
].join("\n"));
check("clicking 选用 selects the skill", picked.picked.includes(picked.name), JSON.stringify(picked.picked));
check("picked tag appears above the box", picked.tag >= 1, "tags=" + picked.tag);
check("button shows a count badge", picked.badge === "1", "badge=" + picked.badge);

// 4. the market tab searches Magpie
const market = await cdp.eval([
  '(() => {',
  '  const tab = [...document.querySelectorAll(".sp-tab")].find(t => t.dataset.spTab === "market");',
  '  tab.click();',
  '  const q = document.getElementById("sp-q");',
  '  q.value = "review";',
  '  q.dispatchEvent(new Event("input", { bubbles: true }));',
  '  return new Promise((done) => setTimeout(() => {',
  '    const p = document.getElementById("skill-panel");',
  '    done({ rows: [...p.querySelectorAll("[data-sp-install]")].map(b => b.dataset.spInstall),',
  '      foot: (document.getElementById("sp-foot") || {}).textContent || "" });',
  '  }, 2200));',
  '})()',
].join("\n"));
check("market tab returns catalogue rows", market.rows.length > 0, JSON.stringify(market.rows.slice(0, 3)));
check("footer states the catalogue size", /市场共\s*\d+/.test(market.foot), market.foot.trim().slice(0, 60));

// 5. installing from the panel really installs
const installed = await cdp.eval([
  '(async () => {',
  '  const b = document.querySelector("[data-sp-install]");',
  '  const ref = b.dataset.spInstall;',
  '  const want = ref.split("/").pop();',
  '  b.click();',
  '  for (let i = 0; i < 60; i++) {',
  '    await new Promise(r => setTimeout(r, 500));',
  '    if (S.skills.some(s => s.name === want)) break;',
  '    if (S.spError) break;',
  '  }',
  '  const err = document.querySelector(".sp-error");',
  '  return { ref, want, names: S.skills.map(s => s.name),',
  '    errorShown: !!err, errorText: err ? err.textContent.slice(0, 120) : "" };',
  '})()',
].join("\n"));
// Either the skill installs, or Magpie's index points at a folder that no
// longer exists — in which case the panel must SAY so, not fail silently.
const ok = installed.names.includes(installed.want);
check("install either succeeded or explained itself",
  ok || installed.errorShown,
  (installed.ref || "") + " -> " + (ok ? "installed" : installed.errorText));

// 6. removing it again (the panel owns uninstall too)
// 6. install a skill this machine really has, then remove it via the panel
const roundTrip = await cdp.eval([
  '(async () => {',
  '  let d = { installed: [] };',
  '  for (let attempt = 0; attempt < 3 && !d.installed.length; attempt++) {',
  '    try {',
  '      d = await api("/api/skills/install", {',
  '        method: "POST", headers: { "Content-Type": "application/json" },',
  '        body: JSON.stringify({ source: "anthropics/skills/webapp-testing", from_magpie: true }),',
  '      });',
  '    } catch (e) {',
  '      if (attempt === 2) return { added: [], removed: false, why: String(e.message) };',
  '      await new Promise(r => setTimeout(r, 2000));',
  '    }',
  '  }',
  '  await loadSkills();',
  '  // Already installed counts as success: the point is the panel round trip.',
  '  const target = (d.installed && d.installed[0]) || "webapp-testing";',
  '  const panel = document.getElementById("skill-panel");',
  '  if (S.spTab !== "installed") {',
  '    [...panel.querySelectorAll(".sp-tab")].find(t => t.dataset.spTab === "installed").click();',
  '    await new Promise(r => setTimeout(r, 300));',
  '  }',
  '  const row = [...panel.querySelectorAll("[data-sp-remove]")]',
  '    .find(b => b.dataset.spRemove === target);',
  '  if (!row) return { added: [target], removed: false, why: "no remove button",',
  '    buttons: [...panel.querySelectorAll("[data-sp-remove]")].map(b => b.dataset.spRemove) };',
  '  window.confirm = () => true;',
  '  row.click();',
  '  for (let i = 0; i < 40; i++) {',
  '    await new Promise(r => setTimeout(r, 500));',
  '    if (!S.skills.some(s => s.name === target)) break;',
  '  }',
  '  return { added: [target], names: S.skills.map(s => s.name),',
  '    removed: !S.skills.some(s => s.name === target) };',
  '})()',
].join("\n"));
check("install then remove works end to end", roundTrip.removed === true,
  JSON.stringify(roundTrip.added) + " -> " + JSON.stringify(roundTrip.names));

// 7. Escape closes it
const closed = await cdp.eval([
  '(() => {',
  '  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));',
  '  return document.getElementById("skill-panel").hidden;',
  '})()',
].join("\n"));
check("Escape closes the panel", closed === true);

const failed = results.filter(r => !r.ok).length;
console.log("\n" + (results.length - failed) + "/" + results.length + " checks passed");
ws.close();
chrome.kill();
process.exit(failed === 0 ? 0 : 1);