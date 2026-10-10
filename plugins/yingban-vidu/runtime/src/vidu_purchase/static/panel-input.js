"use strict";

// UI-only input forwarding. Never inspect the remote page or persist typed text.
function attachPanelInput({canvas, keyboard, send, isLive}) {
  let gesture = null, composing = false;
  const bounded = value => Math.max(-1500, Math.min(1500, value));
  function coordinates(event) {
    const bounds = canvas.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(canvas.width - 1, (event.clientX - bounds.left) / bounds.width * canvas.width)),
      y: Math.max(0, Math.min(canvas.height - 1, (event.clientY - bounds.top) / bounds.height * canvas.height)),
    };
  }
  canvas.addEventListener("pointerdown", event => {
    if (!isLive() || event.button !== 0) return;
    event.preventDefault();
    if (event.pointerType !== "touch") {
      keyboard.focus({preventScroll:true});
      send({kind:"click", ...coordinates(event)});
      return;
    }
    if (gesture || event.isPrimary === false) { gesture = null; return; }
    gesture = {id:event.pointerId, x:event.clientX, y:event.clientY, point:coordinates(event)};
    canvas.setPointerCapture?.(event.pointerId);
  });
  canvas.addEventListener("pointerup", event => {
    if (!gesture || event.pointerId !== gesture.id) return;
    const start = gesture; gesture = null;
    if (!isLive()) return;
    event.preventDefault();
    const dx = start.x - event.clientX, dy = start.y - event.clientY;
    if (Math.hypot(dx, dy) < 10) {
      // A separate keyboard control prevents every touch from opening the software keyboard.
      send({kind:"click", ...coordinates(event)});
    } else {
      const bounds = canvas.getBoundingClientRect();
      send({kind:"wheel", ...start.point,
        delta_x:bounded(dx * canvas.width / bounds.width),
        delta_y:bounded(dy * canvas.height / bounds.height)});
    }
  });
  canvas.addEventListener("pointercancel", () => { gesture = null; });
  canvas.addEventListener("wheel", event => {
    if (!isLive()) return;
    event.preventDefault();
    const scale = event.deltaMode === 1 ? 24 : event.deltaMode === 2 ? 600 : 1;
    send({kind:"wheel", ...coordinates(event),
      delta_x:bounded(event.deltaX * scale), delta_y:bounded(event.deltaY * scale)});
  }, {passive:false});
  const keys = new Set(["Enter","Tab","Backspace","Delete","Escape","ArrowLeft",
    "ArrowUp","ArrowRight","ArrowDown","Home","End","PageUp","PageDown"]);
  keyboard.addEventListener("keydown", event => {
    if (!isLive() || composing || event.isComposing) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
      event.preventDefault(); send({kind:"key", select_all:true});
    } else if (keys.has(event.key) && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault(); send({kind:"key", key:event.key, shift:event.shiftKey});
    } else if ((event.ctrlKey || event.metaKey || event.altKey) && event.key.toLowerCase() !== "v") {
      event.preventDefault();
    }
  });
  keyboard.addEventListener("beforeinput", event => {
    if (!isLive() || composing || event.isComposing || event.defaultPrevented) return;
    const key = {deleteContentBackward:"Backspace", deleteContentForward:"Delete",
      insertLineBreak:"Enter", insertParagraph:"Enter"}[event.inputType];
    if (key) { event.preventDefault(); send({kind:"key", key}); }
  });
  function typed() {
    if (composing || !isLive()) return;
    const text = keyboard.value; keyboard.value = "";
    const clean = text.replace(/[\u0000-\u001f]/g, "").slice(0, 2048);
    if (clean) send({kind:"text", text:clean});
  }
  keyboard.addEventListener("compositionstart", () => { composing = true; });
  keyboard.addEventListener("compositionend", () => { composing = false; typed(); });
  keyboard.addEventListener("input", typed);
  return {reset() { gesture = null; composing = false; keyboard.value = ""; }};
}

if (typeof module !== "undefined") module.exports = {attachPanelInput};
else globalThis.YingbanPanelInput = {attachPanelInput};
