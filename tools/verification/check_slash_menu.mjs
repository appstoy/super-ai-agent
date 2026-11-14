// Drive a real headless Chrome over CDP and check the "/" menu actually opens.
// Static tests can prove the regex; only a browser proves the wiring.
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9333;
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

check("menu hidden on load", await cdp.eval(
  'document.getElementById("slash-menu").hidden === true'));

const skillCount = await cdp.eval("S.skills.length");
check("skills loaded from /api/skills", skillCount === 2, "count=" + skillCount);

const afterSlash = await cdp.eval(
  '(() => {' +
  '  const el = document.getElementById("input");' +
  '  el.focus(); el.value = "/"; el.selectionStart = el.selectionEnd = 1;' +
  '  el.dispatchEvent(new Event("input", { bubbles: true }));' +
  '  const m = document.getElementById("slash-menu");' +
  '  return { hidden: m.hidden, items: m.querySelectorAll("[data-slash]").length,' +
  '           names: [...m.querySelectorAll("[data-slash]")].map(b => b.dataset.slash) };' +
  '})()');
check("typing / opens the menu", afterSlash.hidden === false, "hidden=" + afterSlash.hidden);
check("menu lists every skill", afterSlash.items === 2, JSON.stringify(afterSlash.names));

const filtered = await cdp.eval(
  '(() => {' +
  '  const el = document.getElementById("input");' +
  '  el.value = "/rel"; el.selectionStart = el.selectionEnd = 4;' +
  '  el.dispatchEvent(new Event("input", { bubbles: true }));' +
  '  const m = document.getElementById("slash-menu");' +
  '  return { hidden: m.hidden,' +
  '           names: [...m.querySelectorAll("[data-slash]")].map(b => b.dataset.slash) };' +
  '})()');
check("typing /rel filters to release-notes",
  filtered.names.length === 1 && filtered.names[0] === "release-notes",
  JSON.stringify(filtered.names));

const picked = await cdp.eval(
  '(() => {' +
  '  const el = document.getElementById("input");' +
  '  const ev = (key) => new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });' +
  '  el.dispatchEvent(ev("ArrowDown"));' +
  '  el.dispatchEvent(ev("ArrowDown"));' +
  '  el.dispatchEvent(ev("Enter"));' +
  '  return { value: el.value,' +
  '    hidden: document.getElementById("slash-menu").hidden,' +
  '    picked: [...S.pickedSkills],' +
  '    tag: document.querySelectorAll("#picked-row .picked-tag").length };' +
  '})()');
check("Enter picks the skill", picked.picked.includes("release-notes"), JSON.stringify(picked.picked));
check("command text is stripped", picked.value.trim() === "", JSON.stringify(picked.value));
check("menu closes after picking", picked.hidden === true);
check("picked row shows the tag", picked.tag === 1, "tags=" + picked.tag);

const prose = await cdp.eval(
  '(() => {' +
  '  const el = document.getElementById("input");' +
  '  S.pickedSkills.clear(); renderSkills();' +
  '  el.value = "看 http://example.com/x";' +
  '  el.selectionStart = el.selectionEnd = el.value.length;' +
  '  el.dispatchEvent(new Event("input", { bubbles: true }));' +
  '  return { hidden: document.getElementById("slash-menu").hidden };' +
  '})()');
check("slash inside a URL does not open the menu", prose.hidden === true);

const escaped = await cdp.eval(
  '(() => {' +
  '  const el = document.getElementById("input");' +
  '  el.value = "/"; el.selectionStart = el.selectionEnd = 1;' +
  '  el.dispatchEvent(new Event("input", { bubbles: true }));' +
  '  const opened = !document.getElementById("slash-menu").hidden;' +
  '  el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));' +
  '  return { opened, closed: document.getElementById("slash-menu").hidden };' +
  '})()');
check("Escape closes the menu", escaped.opened && escaped.closed,
  "opened=" + escaped.opened + " closed=" + escaped.closed);

const submitted = await cdp.eval(
  '(async () => {' +
  '  let captured = null;' +
  '  const real = window.fetch;' +
  '  window.fetch = (url, init) => {' +
  '    if (String(url).includes("/api/tasks/async") && init && init.body) {' +
  '      try { captured = JSON.parse(init.body); } catch (e) {}' +
  '    }' +
  '    return Promise.resolve(new Response(JSON.stringify({ dedupe_key: "x", thread_id: "y" }),' +
  '      { status: 202, headers: { "Content-Type": "application/json" } }));' +
  '  };' +
  '  const el = document.getElementById("input");' +
  '  el.value = "写个页面"; el.selectionStart = el.selectionEnd = el.value.length;' +
  '  S.pickedSkills.clear(); S.pickedSkills.add("html-page"); renderSkills();' +
  '  document.getElementById("composer").dispatchEvent(' +
  '    new Event("submit", { bubbles: true, cancelable: true }));' +
  '  await new Promise(r => setTimeout(r, 400));' +
  '  window.fetch = real;' +
  '  return captured;' +
  '})()');
check("payload carries skills",
  !!submitted && Array.isArray(submitted.skills) && submitted.skills.includes("html-page"),
  JSON.stringify(submitted && submitted.skills));

const failed = results.filter(r => !r.ok).length;
console.log("\n" + (results.length - failed) + "/" + results.length + " checks passed");
ws.close();
chrome.kill();
process.exit(failed === 0 ? 0 : 1);
