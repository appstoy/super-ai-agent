// Prove a real browser pops a notification when a task finishes on a hidden tab.
//
// The static tests (tests/e2e/test_task_page_notify.py) assert what the page
// encodes; this drives an actual Chrome over CDP and observes a constructed
// Notification. Run it like this:
//
//   chrome --remote-debugging-port=9222 --user-data-dir=<temp> --headless=new
//   node tools/verification/check_browser_notify.js
//
// It grants the permission through CDP (nobody can click the prompt in
// automation), stubs window.Notification to record instances, forces the tab
// hidden, submits a real task through the page's own submit(), and requires
// exactly one notification - not zero, not two.
const CDP = "http://127.0.0.1:9222";
const PAGE = "http://127.0.0.1:8000/";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const list = await (await fetch(CDP + "/json/list")).json();
  const page = list.find((t) => t.type === "page");
  if (!page) { console.log("FAIL: no page target"); process.exit(1); }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  };
  const call = (method, params = {}) => new Promise((resolve) => {
    const n = ++id;
    pending.set(n, resolve);
    ws.send(JSON.stringify({ id: n, method, params }));
  });
  const ev = async (expr) => {
    const r = await call("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    return r && r.result && r.result.result ? r.result.result.value : undefined;
  };

  await call("Page.enable");
  await call("Runtime.enable");
  await call("Browser.grantPermissions", { origin: "http://127.0.0.1:8000", permissions: ["notifications"] });
  await call("Page.navigate", { url: PAGE });
  await sleep(4500);

  console.log("permission:", await ev("Notification.permission"));

  await ev("window.__notifs=[]; (function(){var R=window.Notification; function S(t,o){window.__notifs.push({title:t,body:(o||{}).body}); return {close:function(){},set onclick(v){}};} S.permission=R.permission; S.requestPermission=function(){return Promise.resolve('granted');}; window.Notification=S;})(); 1");

  await ev("Object.defineProperty(document,'visibilityState',{get:function(){return 'hidden';},configurable:true}); Object.defineProperty(document,'hidden',{get:function(){return true;},configurable:true}); document.dispatchEvent(new Event('visibilitychange')); 1");
  console.log("visibilityState:", await ev("document.visibilityState"));

  await ev("document.getElementById('input').value='用一句话说：通知实测'; submit('用一句话说：通知实测'); 1");

  let count = 0;
  for (let i = 0; i < 60; i++) {
    await sleep(2000);
    count = await ev("window.__notifs.length");
    if (count > 0) break;
  }
  console.log("notifications:", await ev("JSON.stringify(window.__notifs)"));
  await sleep(8000);
  const after = await ev("window.__notifs.length");
  console.log("count after settling:", after);
  console.log(after === 1 ? "PASS: exactly one notification" : "FAIL: " + after);
  ws.close();
  process.exit(after === 1 ? 0 : 1);
}
main().catch((e) => { console.log("ERROR", e.message); process.exit(1); });
