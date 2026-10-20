// Report content growth to MCP Apps hosts. Fixed-height panes scroll inside
// the view; flexible inline views ask the host for their measured height.
let layoutObserver = null;
let layoutFrame = null;
let layoutDimensions = {};
let lastLayoutSize = null;
function reportLayoutSize() {
  layoutFrame = null;
  const content = document.getElementById("panel-content");
  const padding = getComputedStyle(document.body);
  const naturalHeight = Math.ceil(content.getBoundingClientRect().height +
    parseFloat(padding.paddingTop) + parseFloat(padding.paddingBottom));
  const height = Number.isFinite(layoutDimensions.height) ? window.innerHeight :
    Math.min(naturalHeight, layoutDimensions.maxHeight || naturalHeight);
  const size = { width: document.documentElement.clientWidth, height };
  if (lastLayoutSize?.width === size.width && lastLayoutSize?.height === size.height) return;
  lastLayoutSize = size;
  hostNotify("ui/notifications/size-changed", size);
}
function queueLayoutSize() {
  if (layoutFrame === null) layoutFrame = requestAnimationFrame(reportLayoutSize);
}
function updateLayout(context) {
  if (context?.containerDimensions) layoutDimensions = context.containerDimensions;
  const fixed = Number.isFinite(layoutDimensions.height);
  document.documentElement.style.height = fixed ? "100vh" : "";
  document.body.style.height = fixed ? "100%" : "";
  document.body.style.maxHeight = !fixed && layoutDimensions.maxHeight ? `${layoutDimensions.maxHeight}px` : "";
  document.body.style.overflowY = "auto";
  if (!layoutObserver) {
    layoutObserver = new ResizeObserver(queueLayoutSize);
    layoutObserver.observe(document.getElementById("panel-content"));
  }
  queueLayoutSize();
}
function stopLayout() {
  layoutObserver?.disconnect();
  layoutObserver = null;
  if (layoutFrame !== null) cancelAnimationFrame(layoutFrame);
  layoutFrame = null;
}
window.addEventListener("pagehide", stopLayout);
