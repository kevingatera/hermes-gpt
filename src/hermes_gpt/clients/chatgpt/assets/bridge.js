// MCP Apps bridge. The component has no endpoint, credentials, or direct fetch.
const pending = new Map();
let sequence = 0;
function hostRequest(method, params, timeout = 45000) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id);
      const error = new Error("The connection did not respond. Check recent requests before sending again.");
      error.ambiguous = method === "tools/call";
      reject(error);
    }, timeout);
    pending.set(id, { resolve, reject, timer });
    // The host's iframe origin is assigned by ChatGPT. Accept replies only
    // from our parent, which also owns authorization and tool confirmation.
    window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
  });
}
function hostNotify(method, params) {
  window.parent.postMessage({ jsonrpc: "2.0", method, params }, "*");
}
window.addEventListener("message", event => {
  if (event.source !== window.parent || event.data?.jsonrpc !== "2.0") return;
  const message = event.data;
  if (typeof message.id === "number" && pending.has(message.id)) {
    const request = pending.get(message.id);
    clearTimeout(request.timer);
    pending.delete(message.id);
    if (message.error) request.reject(new Error(message.error.message || "The host rejected this request."));
    else request.resolve(message.result);
  } else if (message.method === "ui/notifications/tool-result") {
    applyToolResult(message.params);
  } else if (message.method === "ui/notifications/host-context-changed") {
    applyTheme(message.params);
    updateLayout(message.params);
  } else if (message.method === "ui/resource-teardown" && message.id !== undefined) {
    stopPolling();
    stopLayout();
    window.parent.postMessage({ jsonrpc: "2.0", id: message.id, result: {} }, "*");
  }
});
function decodeResult(result) {
  if (result?.structuredContent) return result.structuredContent;
  for (const item of result?.content || []) {
    if (item.type !== "text") continue;
    try { return JSON.parse(item.text); } catch { /* Plain-text results are not state updates. */ }
  }
  return {};
}
async function callTool(name, args) {
  const result = await hostRequest("tools/call", { name, arguments: args });
  const body = decodeResult(result);
  if (result?.isError || body.success === false || body.ok === false) {
    const error = new Error(body.error?.safe_message || body.error?.message || body.safe_message || body.code || "Hermes could not complete this request.");
    error.traceId = result?._meta?.hermes_request_id;
    throw error;
  }
  return body;
}
async function connectHost() {
  const result = await hostRequest("ui/initialize", {
    appInfo: { name: "hermes-console", version: "0.1.0" },
    appCapabilities: {}, protocolVersion: "2026-01-26"
  });
  applyTheme(result?.hostContext);
  hostNotify("ui/notifications/initialized", {});
  updateLayout(result?.hostContext);
}
