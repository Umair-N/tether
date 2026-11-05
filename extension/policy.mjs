// Exact shared policy, verified by test_mac_browser.
export const ALLOWED = new Set([
  "Accessibility.disable",
  "Accessibility.enable",
  "Accessibility.getChildAXNodes",
  "Accessibility.getFullAXTree",
  "Accessibility.getPartialAXTree",
  "Accessibility.getRootAXNode",
  "Accessibility.queryAXTree",
  "DOM.collectClassNamesFromSubtree",
  "DOM.describeNode",
  "DOM.disable",
  "DOM.enable",
  "DOM.focus",
  "DOM.getBoxModel",
  "DOM.getContentQuads",
  "DOM.getDocument",
  "DOM.getFlattenedDocument",
  "DOM.getNodeForLocation",
  "DOM.getOuterHTML",
  "DOM.pushNodesByBackendIdsToFrontend",
  "DOM.querySelector",
  "DOM.querySelectorAll",
  "DOM.removeAttribute",
  "DOM.requestChildNodes",
  "DOM.requestNode",
  "DOM.resolveNode",
  "DOM.scrollIntoViewIfNeeded",
  "DOM.setAttributeValue",
  "DOM.setOuterHTML",
  "Emulation.clearDeviceMetricsOverride",
  "Emulation.clearGeolocationOverride",
  "Emulation.clearIdleOverride",
  "Emulation.setAutomationOverride",
  "Emulation.setCPUThrottlingRate",
  "Emulation.setDefaultBackgroundColorOverride",
  "Emulation.setDeviceMetricsOverride",
  "Emulation.setEmitTouchEventsForMouse",
  "Emulation.setEmulatedMedia",
  "Emulation.setEmulatedVisionDeficiency",
  "Emulation.setFocusEmulationEnabled",
  "Emulation.setGeolocationOverride",
  "Emulation.setIdleOverride",
  "Emulation.setLocaleOverride",
  "Emulation.setScriptExecutionDisabled",
  "Emulation.setScrollbarsHidden",
  "Emulation.setTimezoneOverride",
  "Emulation.setTouchEmulationEnabled",
  "Emulation.setUserAgentOverride",
  "Emulation.setVirtualTimePolicy",
  "Fetch.continueRequest",
  "Fetch.continueResponse",
  "Fetch.continueWithAuth",
  "Fetch.disable",
  "Fetch.enable",
  "Fetch.failRequest",
  "Fetch.fulfillRequest",
  "Fetch.getResponseBody",
  "Fetch.takeResponseBodyAsStream",
  "Input.dispatchKeyEvent",
  "Input.dispatchMouseEvent",
  "Input.dispatchTouchEvent",
  "Input.imeSetComposition",
  "Input.insertText",
  "Input.setIgnoreInputEvents",
  "Input.synthesizePinchGesture",
  "Input.synthesizeScrollGesture",
  "Input.synthesizeTapGesture",
  "Log.clear",
  "Log.disable",
  "Log.enable",
  "Log.startViolationsReport",
  "Log.stopViolationsReport",
  "Network.disable",
  "Network.emulateNetworkConditions",
  "Network.emulateNetworkConditionsByRule",
  "Network.enable",
  "Network.getRequestPostData",
  "Network.getResponseBody",
  "Network.overrideNetworkState",
  "Network.setBlockedURLs",
  "Network.setBypassServiceWorker",
  "Network.setCacheDisabled",
  "Network.setExtraHTTPHeaders",
  "Network.setUserAgentOverride",
  "Page.addScriptToEvaluateOnNewDocument",
  "Page.bringToFront",
  "Page.captureScreenshot",
  "Page.close",
  "Page.createIsolatedWorld",
  "Page.disable",
  "Page.enable",
  "Page.getFrameTree",
  "Page.getLayoutMetrics",
  "Page.getNavigationHistory",
  "Page.getResourceContent",
  "Page.getResourceTree",
  "Page.handleJavaScriptDialog",
  "Page.navigate",
  "Page.navigateToHistoryEntry",
  "Page.printToPDF",
  "Page.reload",
  "Page.removeScriptToEvaluateOnNewDocument",
  "Page.screencastFrameAck",
  "Page.setDocumentContent",
  "Page.setInterceptFileChooserDialog",
  "Page.setLifecycleEventsEnabled",
  "Page.setWebLifecycleState",
  "Page.startScreencast",
  "Page.stopLoading",
  "Page.stopScreencast",
  "Performance.disable",
  "Performance.enable",
  "Performance.getMetrics",
  "Runtime.addBinding",
  "Runtime.awaitPromise",
  "Runtime.callFunctionOn",
  "Runtime.compileScript",
  "Runtime.disable",
  "Runtime.discardConsoleEntries",
  "Runtime.enable",
  "Runtime.evaluate",
  "Runtime.getIsolateId",
  "Runtime.getProperties",
  "Runtime.globalLexicalScopeNames",
  "Runtime.releaseObject",
  "Runtime.releaseObjectGroup",
  "Runtime.removeBinding",
  "Runtime.runIfWaitingForDebugger",
  "Runtime.runScript"
]);
export class Refusal extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
export function refuse(code, message) { throw new Refusal(code, message); }
export function object(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
function keys(p, allowed) {
  if (!object(p) || Object.keys(p).some(k => !allowed.includes(k)))
    refuse('BAD_PARAMS', 'Unexpected parameter or non-object parameters');
}
export function webURL(value) {
  if (value === 'about:blank') return value;
  if (typeof value !== 'string' || value.length > 8192) refuse('BAD_URL', 'Expected an HTTP(S) URL');
  let u;
  try { u = new URL(value); } catch { refuse('BAD_URL', 'Invalid URL'); }
  if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password)
    refuse('BAD_URL', 'Only HTTP(S) URLs without credentials are allowed');
  return u.href;
}
export function request(r, epoch) {
  keys(r, ['type', 'id', 'reason', 'minutes', 'url', 'epoch']);
  if (r.epoch !== epoch) refuse('OLD_EPOCH', 'Request belongs to another connection');
  if (typeof r.id !== 'string' || !/^[a-zA-Z0-9_-]{1,80}$/.test(r.id) ||
      typeof r.reason !== 'string' || !r.reason.trim() || r.reason.length > 300 ||
      !Number.isInteger(r.minutes) || r.minutes < 1 || r.minutes > 60)
    refuse('BAD_REQUEST', 'Expected id, reason, and integer minutes from 1 to 60');
  return {...r, url: r.url == null ? undefined : webURL(r.url)};
}
export function command(c, g, epoch, now = Date.now()) {
  if (!g) refuse('NO_GRANT', 'A new local approval is required');
  if (now >= g.expires) refuse('EXPIRED', 'Grant expired; approve a new request');
  if (c.epoch !== epoch || c.epoch !== g.epoch) refuse('OLD_EPOCH', 'Command belongs to another connection');
  if (c.grant !== g.id) refuse('WRONG_GRANT', 'Grant id does not match');
  if (c.tab !== g.tab) refuse('WRONG_TAB', 'Only the locally created task tab is allowed');
  keys(c, ['type', 'id', 'grant', 'epoch', 'tab', 'method', 'params']);
  if (typeof c.id !== 'string' || !/^[a-zA-Z0-9_-]{1,80}$/.test(c.id))
    refuse('BAD_COMMAND', 'A bounded command id is required');
  const p = c.params ?? {};
  if (!ALLOWED.has(c.method)) refuse('METHOD_DENIED', 'Page method is not allowed');
  if (!object(p)) refuse('BAD_PARAMS', 'Expected parameter object');
  function check(value) {
    if (Array.isArray(value)) { value.forEach(check); return; }
    if (object(value)) for (const [key, item] of Object.entries(value)) {
      if (['targetId', 'sessionId', 'browserContextId'].includes(key) &&
          !(c.method === 'Page.screencastFrameAck' && key === 'sessionId' && value === p))
        refuse('BAD_PARAMS', 'No target or session switches');
      check(item);
    }
  }
  check(p);
  if (c.method === 'Page.navigate') return {...p, url: webURL(p.url)};
  return p;
}
