/** HTTP client for your agent backend or authenticated UI. No payment or generation auto-retry. */
export class YingbanError extends Error {
  constructor(code, message, status = 0) {
    super(message);
    this.name = "YingbanError";
    this.code = code;
    this.status = status;
  }
}

function resourceId(value) {
  if (typeof value !== "string" || !/^[a-f0-9]{32}$/.test(value)) {
    throw new YingbanError("invalid_id", "Use the ID returned by the service.");
  }
  return value;
}

function requestKey(value) {
  if (typeof value !== "string" || value.length < 8 || value.length > 128 || /[\r\n]/.test(value)) {
    throw new YingbanError("invalid_idempotency_key", "Reuse an 8–128 character key for this request.");
  }
  return value;
}

function decodeEvent(record) {
  let event = "message";
  const lines = [];
  for (const line of record.split(/\r\n|\n|\r/)) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    if (field === "data") lines.push(value);
  }
  if (!lines.length) return null;
  try {
    return { event, data: JSON.parse(lines.join("\n")) };
  } catch {
    throw new YingbanError("invalid_event", "The service returned an invalid event.");
  }
}

export class YingbanClient {
  /** Render panel_url only in the authenticated user's UI, never in model context or logs. */
  openOfficialPanel(purpose = "account", purchaseId = null) {
    const body = {purpose};
    if (purpose === "payment") body.purchase_id = resourceId(purchaseId);
    else if (purpose !== "account" || purchaseId !== null) throw new YingbanError("invalid_input", "Invalid panel purpose.");
    return this.request("/v1/browser-panels", {method:"POST", body});
  }
  constructor({ baseUrl, getAccessToken, fetchImpl = globalThis.fetch, timeoutMs = 30_000 }) {
    const url = new URL(baseUrl);
    if (
      url.username || url.password || url.search || url.hash || url.pathname !== "/" ||
      !(url.protocol === "https:" || (
        url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
      ))
    ) {
      throw new YingbanError("invalid_origin", "Use an HTTPS origin (HTTP only on loopback).");
    }
    if (typeof getAccessToken !== "function" || typeof fetchImpl !== "function") {
      throw new TypeError("getAccessToken and fetchImpl must be functions.");
    }
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new TypeError("Invalid timeout.");
    this.baseUrl = url.origin;
    this.getAccessToken = getAccessToken;
    this.fetchImpl = fetchImpl;
    this.timeoutMs = timeoutMs;
  }

  async response(path, { method = "GET", body, key, signal, stream = false } = {}) {
    const token = await this.getAccessToken();
    if (typeof token !== "string" || !token || /[\r\n]/.test(token)) {
      throw new YingbanError("authentication_required", "Sign in to your agent first.");
    }
    const headers = { Authorization: `Bearer ${token}`, Accept: stream ? "text/event-stream" : "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (key !== undefined) headers["Idempotency-Key"] = requestKey(key);
    const timeout = AbortSignal.timeout(stream ? 90_000 : this.timeoutMs);
    const response = await this.fetchImpl(this.baseUrl + path, {
      method, headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      redirect: "error", cache: "no-store", credentials: "omit",
    });
    if (!response.ok) {
      let error;
      try { error = (await response.json()).error; } catch { /* Do not echo raw upstream data. */ }
      throw new YingbanError(
        typeof error?.code === "string" ? error.code : "http_error",
        typeof error?.message === "string" ? error.message : "The request was not completed.",
        response.status,
      );
    }
    return response;
  }

  async request(path, options) {
    const response = await this.response(path, options);
    try { return await response.json(); } catch {
      throw new YingbanError("invalid_response", "The service returned invalid JSON.", response.status);
    }
  }

  capabilities(options) { return this.request("/v1/capabilities", options); }
  account(options) { return this.request("/v1/account", options); }
  accountConnection(options) { return this.request("/v1/account/connection", options); }
  connectAccount({ signal } = {}) {
    return this.request("/v1/account/connection", { method: "POST", signal });
  }
  commerceSession(options) { return this.request("/v1/commerce/session", options); }
  websiteOrders(options) { return this.request("/v1/commerce/orders", options); }
  // Pass only the user's stated choices. Explicit delegation enables recommendations.
  prepareVideoRequest(request, idempotencyKey, { signal } = {}) {
    return this.request("/v1/video-briefs", {
      method: "POST", body: request, key: requestKey(idempotencyKey), signal,
    });
  }
  getVideoBrief(briefId, options) {
    return this.request(`/v1/video-briefs/${resourceId(briefId)}`, options);
  }
  answerVideoQuestions(briefId, answers, { signal } = {}) {
    return this.request(`/v1/video-briefs/${resourceId(briefId)}`, {
      method: "PATCH", body: answers, signal,
    });
  }
  listPlans(options) { return this.request("/v1/plans", options); }
  preparePurchase(quoteId, idempotencyKey, { signal } = {}) {
    return this.request("/v1/purchases", {
      method: "POST", body: { quote_id: resourceId(quoteId) }, key: requestKey(idempotencyKey), signal,
    });
  }
  checkPurchase(purchaseId, options) {
    return this.request(`/v1/purchases/${resourceId(purchaseId)}`, options);
  }
  startVideo(config, idempotencyKey, { signal } = {}) {
    return this.request("/v1/videos", { method: "POST", body: { config }, key: requestKey(idempotencyKey), signal });
  }
  getWorkflow(workflowId, options) { return this.request(`/v1/videos/${resourceId(workflowId)}`, options); }
  updateVideo(workflowId, config, revision, { signal } = {}) {
    return this.request(`/v1/videos/${resourceId(workflowId)}`, { method: "PATCH", body: { config, revision }, signal });
  }
  recommendPlans(workflowId, { signal } = {}) {
    return this.request(`/v1/videos/${resourceId(workflowId)}/recommend`, { method: "POST", signal });
  }
  purchaseForVideo(workflowId, quoteId, revision, { signal } = {}) {
    return this.request(`/v1/videos/${resourceId(workflowId)}/purchase`, {
      method: "POST", body: { quote_id: resourceId(quoteId), revision }, signal,
    });
  }
  cancelVideo(workflowId, revision, { signal } = {}) {
    return this.request(`/v1/videos/${resourceId(workflowId)}/cancel`, { method: "POST", body: { revision }, signal });
  }
  // Only call in response to a new user decision after displaying current config and cost.
  confirmGeneration(workflowId, confirmationId, { signal } = {}) {
    return this.request(`/v1/videos/${resourceId(workflowId)}/confirm`, {
      method: "POST", body: { confirmation_id: resourceId(confirmationId) }, signal,
    });
  }

  /** One bounded stream. Reconnect the SAME purchase ID with a fresh token if still pending. */
  async *purchaseEvents(purchaseId, { signal } = {}) {
    const response = await this.response(`/v1/purchases/${resourceId(purchaseId)}/events`, { signal, stream: true });
    if (!response.headers.get("content-type")?.startsWith("text/event-stream") || !response.body) {
      throw new YingbanError("invalid_stream", "Expected a purchase event stream.");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        if (buffer.length > 1_048_576) throw new YingbanError("event_too_large", "Event exceeds size limit.");
        let delimiter;
        while ((delimiter = /\r\n\r\n|\n\n|\r\r/.exec(buffer))) {
          const result = decodeEvent(buffer.slice(0, delimiter.index));
          buffer = buffer.slice(delimiter.index + delimiter[0].length);
          if (result) yield result;
        }
        if (done) return; // An incomplete event is never accepted as payment evidence.
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}
