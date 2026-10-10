import assert from "node:assert/strict";
import test from "node:test";
import input from "../src/vidu_purchase/static/panel-input.js";

class Surface extends EventTarget {
  width = 720; height = 800; value = ""; focused = false;
  getBoundingClientRect() { return {left:0, top:0, width:360, height:400}; }
  focus() { this.focused = true; }
  setPointerCapture() {}
  fire(type, properties = {}) {
    const event = new Event(type, {cancelable:true});
    Object.assign(event, properties); this.dispatchEvent(event); return event;
  }
}
function setup() {
  const canvas = new Surface(), keyboard = new Surface(), sent = [];
  const control = input.attachPanelInput({canvas, keyboard, send:value => sent.push(value), isLive:() => true});
  const touch = {pointerType:"touch", pointerId:1, isPrimary:true, button:0, clientX:100, clientY:300};
  return {canvas, keyboard, sent, touch, control};
}

test("a phone swipe scrolls without accidentally clicking a login or payment control", () => {
  const {canvas, sent, touch} = setup();
  canvas.fire("pointerdown", touch);
  assert.equal(sent.length, 0);
  canvas.fire("pointerup", {...touch, clientY:100});
  assert.deepEqual(sent, [{kind:"wheel", x:200, y:600, delta_x:0, delta_y:400}]);
});

test("touch tap clicks once, without forcing the software keyboard open", () => {
  const {canvas, keyboard, sent, touch} = setup();
  canvas.fire("pointerdown", touch); canvas.fire("pointerup", touch);
  assert.deepEqual(sent, [{kind:"click", x:200, y:600}]);
  assert.equal(keyboard.focused, false);
});

test("canceled and multi-touch gestures never click after the finger is released", () => {
  const {canvas, sent, touch} = setup();
  canvas.fire("pointerdown", touch); canvas.fire("pointercancel", touch); canvas.fire("pointerup", touch);
  canvas.fire("pointerdown", touch);
  canvas.fire("pointerdown", {...touch, pointerId:2, isPrimary:false});
  canvas.fire("pointerup", touch);
  assert.deepEqual(sent, []);
});

test("phone delete/enter and Chinese composition work without keyboard keydown events", () => {
  const {keyboard, sent, control} = setup();
  keyboard.fire("beforeinput", {inputType:"deleteContentBackward"});
  keyboard.fire("beforeinput", {inputType:"insertLineBreak"});
  keyboard.fire("compositionstart"); keyboard.value = "橘猫";
  keyboard.fire("input"); assert.equal(sent.length, 2);
  keyboard.fire("compositionend"); keyboard.fire("input");
  assert.deepEqual(sent, [{kind:"key", key:"Backspace"}, {kind:"key", key:"Enter"}, {kind:"text", text:"橘猫"}]);
  assert.equal(keyboard.value, "");
  keyboard.value = "temporary"; control.reset(); assert.equal(keyboard.value, "");
});
