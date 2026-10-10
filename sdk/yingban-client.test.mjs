import test from "node:test";
import assert from "node:assert/strict";
import { YingbanClient, YingbanError } from "./yingban-client.mjs";

const id = "a".repeat(32);
function client(fetchImpl, overrides = {}) {
  return new YingbanClient({ baseUrl: "http://127.0.0.1:8787", getAccessToken: () => "test-only", fetchImpl, ...overrides });
}

test("conversation keeps missing preferences empty until user explicitly delegates", async () => {
  const requests = [];
  const api = client(async (url, options) => {
    requests.push({ url, options });
    return Response.json({ brief_id: id, revision: requests.length - 1 });
  });
  const brief = await api.prepareVideoRequest({ prompt: "海景", preferences: { aspect_ratio: "9:16" } }, "one-conversation");
  assert.deepEqual(JSON.parse(requests[0].options.body), { prompt: "海景", preferences: { aspect_ratio: "9:16" } });
  assert.equal(requests[0].options.headers["Idempotency-Key"], "one-conversation");
  await api.getVideoBrief(brief.brief_id);
  const answers = { revision: brief.revision, use_recommended_for: ["model_version", "duration", "resolution"] };
  await api.answerVideoQuestions(brief.brief_id, answers);
  assert.equal(requests[2].options.method, "PATCH");
  assert.deepEqual(JSON.parse(requests[2].options.body), answers);
  assert.ok(requests.every(request => request.url.includes("/v1/video-briefs")));
});

test("request refreshes identity, uses fixed idempotency, and never retries a mutation", async () => {
  let calls = 0, tokens = 0;
  const api = client(async (url, options) => {
    calls++;
    assert.equal(url, "http://127.0.0.1:8787/v1/purchases");
    assert.equal(options.headers.Authorization, "Bearer refreshed-token");
    assert.equal(options.headers["Idempotency-Key"], "fixed-purchase-key");
    assert.equal(options.redirect, "error");
    assert.deepEqual(JSON.parse(options.body), { quote_id: id });
    throw new TypeError("network interrupted");
  }, { getAccessToken: async () => { tokens++; return "refreshed-token"; } });
  await assert.rejects(api.preparePurchase(id, "fixed-purchase-key"), /network interrupted/);
  assert.equal(calls, 1);
  assert.equal(tokens, 1);
});

test("not-configured is a typed error, never a synthetic offer", async () => {
  const api = client(async () => Response.json({ error: { code: "billing_not_configured", message: "未接入" } }, { status: 501 }));
  await assert.rejects(api.listPlans(), error => error instanceof YingbanError && error.status === 501 && error.code === "billing_not_configured");
});

test("SSE handles split UTF-8 and CRLF, and receiving ready does not submit anything", async () => {
  let calls = 0;
  const bytes = new TextEncoder().encode(': ping\r\n\r\nevent: purchase\r\ndata: {"state":"ready","message":"可以使用"}\r\n\r\n');
  const api = client(async (_url, options) => {
    calls++;
    assert.equal(options.method, "GET");
    const stream = new ReadableStream({ start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      controller.close();
    } });
    return new Response(stream, { headers: { "content-type": "text/event-stream" } });
  });
  const events = [];
  for await (const item of api.purchaseEvents(id)) events.push(item);
  assert.deepEqual(events, [{ event: "purchase", data: { state: "ready", message: "可以使用" } }]);
  assert.equal(calls, 1);
});

test("an incomplete payment event is ignored", async () => {
  const api = client(async () => new Response('event: purchase\ndata: {"state":"ready"}', { headers: { "content-type": "text/event-stream" } }));
  const events = [];
  for await (const event of api.purchaseEvents(id)) events.push(event);
  assert.deepEqual(events, []);
});

test("confirm sends only the explicitly selected confirmation and never fabricates it", async () => {
  const api = client(async (url, options) => {
    assert.ok(url.endsWith(`/v1/videos/${id}/confirm`));
    assert.deepEqual(JSON.parse(options.body), { confirmation_id: "b".repeat(32) });
    return Response.json({ state: "generating" });
  });
  assert.equal((await api.confirmGeneration(id, "b".repeat(32))).state, "generating");
  assert.throws(() => api.confirmGeneration("../someone", id), /ID returned/);
  assert.throws(() => client(() => {}, { baseUrl: "http://public.example" }), /HTTPS/);
});
