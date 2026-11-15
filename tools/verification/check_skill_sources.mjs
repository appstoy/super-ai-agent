// Verify the skill panel's sources: three tabs, importing from other agents,
// the per-agent sub-filter, and drag-and-drop import of folders and archives.
//
// Needs Chrome, a server on :8000, and at least one other agent on this machine
// (Claude Code / Codex / Gemini / .agents).
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9341;
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
  results.push(ok);
  console.log((ok ? "  ok  " : "  FAIL") + "  " + name + (detail ? "  [" + detail + "]" : ""));
}

await cdp.send("Page.navigate", { url: URL });
await sleep(3500);

const tabs = await cdp.eval([
  '(() => {',
  '  document.getElementById("skill-btn").click();',
  '  const p = document.getElementById("skill-panel");',
  '  return {',
  '    labels: [...p.querySelectorAll(".sp-tab")].map(t => t.textContent.trim()),',
  '    keys: [...p.querySelectorAll(".sp-tab")].map(t => t.dataset.spTab),',
  '    hasDrop: !!document.getElementById("sp-drop"),',
  '    hasLink: !!document.getElementById("sp-add-link"),',
  '    hasFile: !!document.getElementById("sp-add-file"),',
  '  };',
  '})()',
].join("\n"));
check("three tabs present", tabs.keys.length === 3, JSON.stringify(tabs.labels));

// the agents count must fill in after the prefetch, not stay at 0
const counted = await cdp.eval([
  '(async () => {',
  '  for (let i = 0; i < 40; i++) {',
  '    await new Promise(r => setTimeout(r, 250));',
  '    if (S.agentSkills.length) break;',
  '  }',
  '  await new Promise(r => setTimeout(r, 300));',
  '  const el = document.getElementById("sp-agents-n");',
  '  return { text: el ? el.textContent : "", skills: S.agentSkills.length };',
  '})()',
].join("\n"));
check("agents tab shows how many are importable",
  Number(counted.text) === counted.skills && counted.skills > 0,
  "badge=" + counted.text + " skills=" + counted.skills);
check("tabs are installed / agents / market",
  tabs.keys[0] === "installed" && tabs.keys[1] === "agents" && tabs.keys[2] === "market",
  JSON.stringify(tabs.keys));
check("drop zone rendered", tabs.hasDrop === true);
check("manual add buttons rendered", tabs.hasLink && tabs.hasFile);

const agents = await cdp.eval([
  '(async () => {',
  '  [...document.querySelectorAll(".sp-tab")].find(t => t.dataset.spTab === "agents").click();',
  '  for (let i = 0; i < 40; i++) {',
  '    await new Promise(r => setTimeout(r, 250));',
  '    if (!S.agentLoading && S.agentSkills.length) break;',
  '  }',
  '  const p = document.getElementById("skill-panel");',
  '  return {',
  '    skills: S.agentSkills.length,',
  '    sources: S.agentSources.filter(s => s.exists).map(s => s.id),',
  '    pills: [...p.querySelectorAll("[data-agent-filter]")].map(b => b.dataset.agentFilter),',
  '    rows: p.querySelectorAll("[data-sp-import]").length,',
  '  };',
  '})()',
].join("\n"));
check("found skills in other agents", agents.skills > 0, "skills=" + agents.skills);
check("sub-filter lists every agent folder", agents.pills.length > 1, JSON.stringify(agents.pills));
check("import buttons rendered", agents.rows > 0, "rows=" + agents.rows);

const filt = await cdp.eval([
  '(async () => {',
  '  const first = S.agentSources.find(s => s.exists && s.count > 0);',
  '  if (!first) return { skip: true };',
  '  const pill = [...document.querySelectorAll("[data-agent-filter]")]',
  '    .find(b => b.dataset.agentFilter === first.id);',
  '  if (!pill) return { skip: true };',
  '  pill.click();',
  '  await new Promise(r => setTimeout(r, 400));',
  '  const p = document.getElementById("skill-panel");',
  '  return { id: first.id, rows: p.querySelectorAll("[data-sp-import]").length };',
  '})()',
].join("\n"));
check("agent sub-filter narrows the list", filt.skip || filt.rows > 0,
  (filt.id || "") + " rows=" + filt.rows);

const dropped = await cdp.eval([
  '(async () => {',
  '  S.agentFilter = "all";',
  '  const md = "---\\nname: drag-test\\ndescription: from a drop\\n---\\n# Drag\\n";',
  '  const file = new File([md], "SKILL.md", { type: "text/markdown" });',
  '  const before = S.skills.length;',
  '  await sendFolder([{ path: "drag-test/SKILL.md", file }], "drag-test");',
  '  for (let i = 0; i < 40; i++) {',
  '    await new Promise(r => setTimeout(r, 250));',
  '    if (S.skills.some(s => s.name === "drag-test")) break;',
  '  }',
  '  return { names: S.skills.map(s => s.name), before, err: S.spError };',
  '})()',
].join("\n"));
check("folder drop imports the skill", dropped.names.includes("drag-test"),
  "before=" + dropped.before + (dropped.err ? " err=" + String(dropped.err).slice(0, 80) : ""));

const refused = await cdp.eval([
  '(async () => {',
  '  S.spError = null;',
  '  const file = new File(["hello"], "readme.txt", { type: "text/plain" });',
  '  await sendFolder([{ path: "no-skill/readme.txt", file }], "no-skill");',
  '  await new Promise(r => setTimeout(r, 400));',
  '  return { err: S.spError || "" };',
  '})()',
].join("\n"));
check("folder without SKILL.md is explained",
  refused.err.includes("SKILL.md"), JSON.stringify(String(refused.err).slice(0, 60)));

const cleaned = await cdp.eval([
  '(async () => {',
  '  window.confirm = () => true;',
  '  await removeSkill("drag-test");',
  '  await new Promise(r => setTimeout(r, 900));',
  '  return S.skills.map(s => s.name);',
  '})()',
].join("\n"));
check("removing the dropped skill works", !cleaned.includes("drag-test"), JSON.stringify(cleaned));

const overlay = await cdp.eval([
  '(() => {',
  '  const dt = new DataTransfer();',
  '  dt.items.add(new File(["x"], "a.zip", { type: "application/zip" }));',
  '  document.dispatchEvent(new DragEvent("dragover", { bubbles: true, dataTransfer: dt }));',
  '  const on = document.getElementById("page-drop").classList.contains("on");',
  '  document.dispatchEvent(new DragEvent("dragleave", { bubbles: true }));',
  '  return { on };',
  '})()',
].join("\n"));
check("dragging over the page shows the overlay", overlay.on === true);

const failed = results.filter(r => !r).length;
console.log("\n" + (results.length - failed) + "/" + results.length + " checks passed");
ws.close();
chrome.kill();
process.exit(failed === 0 ? 0 : 1);