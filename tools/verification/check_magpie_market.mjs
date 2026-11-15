// Drive a real headless Chrome and check the Magpie skill market end to end:
// typing a query pulls catalogue rows into the "/" menu, and clicking one
// installs it for real (a live HTTP call against :8000).
//
// Needs Chrome, Magpie's catalogue, and a server on :8000.
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9334;
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

// --- Does the "/" menu offer Magpie catalogue skills, and can one be installed? ---
const found = await cdp.eval([
  "(async () => {",
  '  const el = document.getElementById("input");',
  '  el.focus(); el.value = "/webapp"; el.selectionStart = el.selectionEnd = 7;',
  '  el.dispatchEvent(new Event("input", { bubbles: true }));',
  '  await new Promise(r => setTimeout(r, 2200));',
  '  const m = document.getElementById("slash-menu");',
  '  return { hidden: m.hidden,',
  '    market: [...m.querySelectorAll("[data-market]")].map(b => b.dataset.market),',
  '    section: (m.querySelector(".slash-sec") || {}).textContent || "" };',
  "})()",
].join("\n"));
check("menu opened on /webapp", found.hidden === false);
check("Magpie section rendered", (found.section || "").includes("Magpie"), (found.section || "").trim());
check("market rows present", found.market.length > 0, JSON.stringify(found.market));

const installed = await cdp.eval([
  "(async () => {",
  '  const m = document.getElementById("slash-menu");',
  '  const btn = m.querySelector("[data-market]");',
  '  if (!btn) return { error: "no row" };',
  '  const ref = btn.dataset.market;',
  '  const want = ref.split("/").pop();',
  '  btn.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));',
  '  for (let i = 0; i < 80; i++) {',
  '    await new Promise(r => setTimeout(r, 500));',
  '    if (S.skills.some(s => s.name === want)) break;',
  '  }',
  '  return { ref, want, names: S.skills.map(s => s.name), picked: [...S.pickedSkills] };',
  "})()",
].join("\n"));
check("click installed the skill",
  !!(installed.names && installed.names.includes(installed.want)),
  (installed.ref || "") + " -> " + JSON.stringify(installed.names || installed.error));
check("new skill auto-selected",
  !!(installed.picked && installed.picked.includes(installed.want)),
  JSON.stringify(installed.picked));

const failed = results.filter(r => !r.ok).length;
console.log("\n" + (results.length - failed) + "/" + results.length + " checks passed");
ws.close();
chrome.kill();
process.exit(failed === 0 ? 0 : 1);
