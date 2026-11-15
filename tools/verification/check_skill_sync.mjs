// Verify importing from another agent and that all three pickers stay in sync.
//
// Regression for two bugs: an import button carrying "claude|" (a stray pipe
// in the generated markup) made EVERY agent import fail with "unknown agent",
// and un-picking from 本次套用 did not repaint the panel rows.
//
// Needs Chrome and a server on :8000.
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9343;
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

// 1. the import button carries a clean agent id (no stray pipe)
const ids = await cdp.eval([
  "(async () => {",
  '  document.getElementById("skill-btn").click();',
  '  [...document.querySelectorAll(".sp-tab")].find(t => t.dataset.spTab === "agents").click();',
  '  for (let i = 0; i < 40; i++) {',
  '    await new Promise(r => setTimeout(r, 250));',
  '    if (!S.agentLoading && S.agentSkills.length) break;',
  '  }',
  '  const p = document.getElementById("skill-panel");',
  '  return [...p.querySelectorAll("[data-sp-import]")].map(b => b.dataset.spImport);',
  "})()",
].join("\n"));
const clean = ids.length > 0 && ids.every(id => /^[a-z_]+$/.test(id));
check("every import button has a valid agent id", clean,
  JSON.stringify([...new Set(ids)]));

// 2. clicking an import button really imports (the bug returned "unknown agent: claude|")
const imported = await cdp.eval([
  "(async () => {",
  '  window.confirm = () => true;',
  '  S.pickedSkills.clear();',
  '  const btn = document.querySelector("[data-sp-import]");',
  '  const name = btn.dataset.spName;',
  '  const before = S.skills.length;',
  '  btn.click();',
  '  for (let i = 0; i < 60; i++) {',
  '    await new Promise(r => setTimeout(r, 400));',
  '    if (S.skills.some(s => s.name === name) || S.spError) break;',
  '  }',
  '  // let the import auto-select finish before asserting sync',
  '  await new Promise(r => setTimeout(r, 1200));',
  '  return { name, before, names: S.skills.map(s => s.name), err: S.spError };',
  "})()",
].join("\n"));
check("clicking 导入 succeeds",
  imported.names.includes(imported.name) || imported.names.length > imported.before,
  imported.name + " -> " + JSON.stringify(imported.names.slice(0, 4)) +
  (imported.err ? " err=" + String(imported.err).slice(0, 80) : ""));

// 3. cancel from "本次套用" must also clear the panel rows
const synced = await cdp.eval([
  "(async () => {",
  '  const target = "html-page";',
  '  S.pickedSkills.clear();',
  '  S.pickedSkills.add(target);',
  '  S.spError = null;',
  '  S.spTab = "installed";',
  '  renderSkills();',
  '  renderSkillPanel();',
  '  const beforeTag = document.querySelectorAll("#picked-row .picked-tag").length;',
  '  const beforePicked = document.querySelectorAll("[data-sp-pick].picked").length;',
  '  const x = document.querySelector("#picked-row [data-unpick]");',
  '  x.click();',
  '  await new Promise(r => setTimeout(r, 300));',
  '  return {',
  '    beforeTag, beforePicked,',
  '    afterTag: document.querySelectorAll("#picked-row .picked-tag").length,',
  '    afterPicked: document.querySelectorAll("[data-sp-pick].picked").length,',
  '    picked: [...S.pickedSkills],',
  '    badge: document.getElementById("skill-count").textContent,',
  '  };',
  "})()",
].join("\n"));
check("× clears the tag", synced.beforeTag === 1 && synced.afterTag === 0,
  "before=" + synced.beforeTag + " after=" + synced.afterTag);
check("× also clears the panel selection",
  synced.beforePicked >= 1 && synced.afterPicked === 0,
  "panel picked " + synced.beforePicked + " -> " + synced.afterPicked);
check("× clears the set and badge",
  synced.picked.length === 0 && synced.badge === "0",
  JSON.stringify(synced.picked) + " badge=" + synced.badge);

// 4. picking from the panel must show in 本次套用 too
const reverse = await cdp.eval([
  "(async () => {",
  '  S.pickedSkills.clear();',
  '  renderSkills();',
  '  renderSkillPanel();',
  '  const b = document.querySelector("[data-sp-pick]");',
  '  const name = b.dataset.spPick;',
  '  b.click();',
  '  await new Promise(r => setTimeout(r, 300));',
  '  return {',
  '    name, picked: [...S.pickedSkills],',
  '    tag: document.querySelectorAll("#picked-row .picked-tag").length,',
  '    badge: document.getElementById("skill-count").textContent,',
  '  };',
  "})()",
].join("\n"));
check("panel 选用 shows in 本次套用", reverse.picked.includes(reverse.name) && reverse.tag === 1,
  JSON.stringify(reverse.picked) + " tag=" + reverse.tag + " badge=" + reverse.badge);

// clean up any skill imported by this run
await cdp.eval([
  "(async () => {",
  '  window.confirm = () => true;',
  '  for (const n of ["appstoy-brief"]) {',
  '    if (S.skills.some(s => s.name === n)) await removeSkill(n);',
  '  }',
  '  return true;',
  "})()",
].join("\n"));

const failed = results.filter(r => !r).length;
console.log("\n" + (results.length - failed) + "/" + results.length + " checks passed");
ws.close();
chrome.kill();
process.exit(failed === 0 ? 0 : 1);