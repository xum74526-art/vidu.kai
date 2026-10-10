import assert from "node:assert/strict";
import test from "node:test";
import {routeToolResult, validateInstallation, watchWorkflow} from "./yingban-mobile.mjs";

const origin = "https://video.example";
const id = "a".repeat(32);
const result = state => ({structuredContent:{workflow_id:id, state}});

test("mobile installation refuses an undeployed server or a replaced MCP endpoint", () => {
  const manifest = {schema:"yingban.remote-plugin.v1", id:"yingban-vidu", installable:false};
  assert.throws(() => validateInstallation(manifest, origin), {code:"deployment_not_ready"});
  assert.throws(() => validateInstallation(manifest, "http://127.0.0.1:8787"), {code:"invalid_service"});
  Object.assign(manifest, {installable:true, mcp:{transport:"streamable-http", url:origin + "/mcp"},
    auth:{type:"oauth2", resource:origin + "/mcp", resource_metadata_url:origin + "/.well-known/oauth-protected-resource/mcp"},
    ui:{panel_origin:origin}});
  assert.equal(validateInstallation(manifest, origin).serverUrl, origin + "/mcp");
  manifest.mcp.url = "https://elsewhere.example/mcp";
  assert.throws(() => validateInstallation(manifest, origin), {code:"invalid_manifest"});
});

test("one-time panel ticket stays in UI metadata and cannot reach model payload", () => {
  const tool = {content:[{type:"text", text:"请完成授权"}], structuredContent:{purpose:"account"},
    _meta:{yingbanPanel:{purpose:"account", panel_url:origin + "/panel#ticket=" + "x".repeat(40)}}};
  const routed = routeToolResult(tool, origin);
  assert.ok(routed.panel.url.includes("ticket="));
  assert.ok(!JSON.stringify(routed.model).includes("ticket"));
  assert.ok(!("_meta" in routed.model));
  tool._meta.yingbanPanel.panel_url = "https://evil.example/panel#ticket=" + "x".repeat(40);
  assert.throws(() => routeToolResult(tool, origin), {code:"invalid_panel"});
});

test("foreground resume queries the same task through completion without a submission", async () => {
  const calls = [], states = ["generating", "completed"], updates = [];
  const call = async request => { calls.push(request); return result(states.shift()); };
  for await (const value of watchWorkflow(call, id, {pollMs:500})) updates.push(value.state);
  assert.deepEqual(updates, ["generating", "completed"]);
  assert.deepEqual(calls, Array(2).fill({name:"vidu_get_workflow", arguments:{workflow_id:id}}));
});

test("confirmation, submission uncertainty and failure stop automatic progression", async () => {
  for (const state of ["awaiting_generation_confirmation", "submission_unknown", "failed", "account_required"]) {
    let calls = 0;
    for await (const value of watchWorkflow(async () => { calls++; return result(state); }, id)) {
      assert.equal(value.state, state);
    }
    assert.equal(calls, 1);
  }
});

test("background cancellation stops polling and a network failure never retries a write", async () => {
  const abort = new AbortController();
  let calls = 0;
  const watching = watchWorkflow(async () => { calls++; return result("generating"); }, id, {signal:abort.signal});
  await watching.next(); abort.abort();
  await assert.rejects(watching.next()); assert.equal(calls, 1);
  const failed = watchWorkflow(async () => { throw new Error("offline"); }, id);
  await assert.rejects(failed.next(), /offline/);
  const wrong = watchWorkflow(async () => ({structuredContent:{workflow_id:"b".repeat(32), state:"completed"}}), id);
  await assert.rejects(wrong.next(), {code:"invalid_workflow"});
});
