/** Pi 1.4.0 lifecycle adapter. Shared policy and memory algorithms live in Python. */
import { spawn } from "node:child_process";
import { StringDecoder } from "node:string_decoder";
import { fileURLToPath } from "node:url";
import { VERSION, type ExtensionAPI, type ExtensionContext, type SessionMessageEntry } from "@earendil-works/pi-coding-agent";

const CUSTOM_TYPE = "codex-mem-context";
const MAX_BYTES = 256 * 1024;
const MAX_EVENTS = 256;
const TIMEOUT_MS = 2000;
const ROOT = fileURLToPath(new URL("..", import.meta.url));

type Receipt = { status?: string; reason?: string; text?: string; events?: Receipt[]; omitted_chars?: number };

/** No text or credentials are placed in argv or diagnostics. Each call is finite. */
function request(payload: object): Promise<Receipt> {
  const raw = JSON.stringify({ version: 1, pi_version: VERSION, ...payload });
  if (Buffer.byteLength(raw) > MAX_BYTES) return Promise.resolve({ reason: "request_overflow" });
  return new Promise((resolve) => {
    let output = "";
    let outputBytes = 0;
    const decoder = new StringDecoder("utf8");
    let finished = false;
    const child = spawn(process.env.CODEX_MEM_PYTHON || "python3", ["-m", "codex_mem.pi_host"],
      { cwd: ROOT, stdio: ["pipe", "pipe", "ignore"] });
    const finish = (value: Receipt) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      child.kill("SIGKILL");
      resolve(value);
    };
    const timer = setTimeout(() => finish({ reason: "service_timeout" }), TIMEOUT_MS);
    child.on("error", () => finish({ reason: "service_unavailable" }));
    child.stdin.on("error", () => finish({ reason: "service_unavailable" }));
    child.stdout.on("data", (data: Buffer) => {
      outputBytes += data.length;
      if (outputBytes > 64 * 1024) return finish({ reason: "response_overflow" });
      output += decoder.write(data);
    });
    child.on("close", (code) => {
      if (code !== 0) return finish({ reason: "service_unavailable" });
      try {
        const value: unknown = JSON.parse(output + decoder.end());
        finish(value && typeof value === "object" ? value as Receipt : { reason: "invalid_response" });
      } catch { finish({ reason: "invalid_response" }); }
    });
    child.stdin.end(raw);
  });
}

function record(entry: SessionMessageEntry) {
  const message = entry.message;
  if (message.role !== "user" && message.role !== "assistant" && message.role !== "toolResult") return null;
  // Do not copy images, thinking, provider signatures, tool arguments or usage.
  const text = typeof message.content === "string" ? message.content : message.content
    .filter((block) => block.type === "text").map((block) => block.text).join("\n");
  return { message_id: entry.id, role: message.role, text,
    ...(message.role === "assistant" ? { stop_reason: message.stopReason } : {}),
    ...(message.role === "toolResult" ? { tool_id: message.toolCallId, tool_name: message.toolName } : {}) };
}

export default function (pi: ExtensionAPI) {
  // The installed owning API was checked at this exact version. Unknown APIs fail open.
  if (VERSION !== "1.4.0") {
    pi.on("session_start", async (_event, ctx) => { ctx.ui.setStatus("codex-mem", "Memory unavailable: unsupported Pi version"); });
    return;
  }
  let session = "";
  let cwd = "";
  let anchor: string | null = null;
  let observed = new Set<object>();
  let overflow = false;
  let activeContext: string | undefined;

  const identity = (ctx: ExtensionContext) => ({ project: ctx.cwd, session_id: ctx.sessionManager.getSessionId() });
  const matches = (ctx: ExtensionContext) => ctx.cwd === cwd && ctx.sessionManager.getSessionId() === session;
  const status = (ctx: ExtensionContext, reason: string) => ctx.ui.setStatus("codex-mem", `Memory: ${reason}`);
  const context = async (ctx: ExtensionContext, phase: "start" | "prompt", query = "") => {
    activeContext = undefined;
    const response = await request({ ...identity(ctx), operation: "context", phase, query });
    if (response.status !== "ok" || response.reason) status(ctx, response.reason || "service_unavailable");
    if (typeof response.text !== "string" || response.text.length > 6000 || !response.text) return;
    activeContext = response.text;
    return { customType: CUSTOM_TYPE, content: response.text, display: false };
  };

  pi.on("session_start", async (_event, ctx) => {
    session = ctx.sessionManager.getSessionId();
    cwd = ctx.cwd;
    anchor = ctx.sessionManager.getLeafId();
    observed = new Set();
    overflow = false;
    const message = await context(ctx, "start");
    if (message) pi.sendMessage(message, { triggerTurn: false });
  });
  pi.on("message_end", async (event, ctx) => {
    if (!matches(ctx) || overflow) return;
    const role = event.message.role;
    if (role !== "user" && role !== "assistant" && role !== "toolResult") return;
    observed.add(event.message);
    if (observed.size > MAX_EVENTS) { observed.clear(); overflow = true; }
  });
  pi.on("agent_settled", async (_event, ctx) => {
    if (!matches(ctx)) return;
    const entries: SessionMessageEntry[] = [];
    let cursor = ctx.sessionManager.getLeafId();
    let traversed = 0;
    while (cursor && cursor !== anchor && traversed++ < 1024) {
      const entry = ctx.sessionManager.getEntry(cursor);
      if (!entry) break;
      if (entry.type === "message" && observed.has(entry.message)) entries.push(entry);
      cursor = entry.parentId;
    }
    const complete = !overflow && cursor === anchor && entries.length === observed.size;
    const events = entries.reverse().map(record).filter((entry) => entry !== null);
    observed.clear();
    anchor = ctx.sessionManager.getLeafId();
    overflow = false;
    if (!complete) { status(ctx, "capture omitted: event limit or native identity unavailable"); return; }
    if (!events.length) return;
    const response = await request({ ...identity(ctx), operation: "capture", settled: true, events });
    if (response.status !== "ok") status(ctx, response.reason || "service_unavailable");
    else if (response.events?.some((event) => event.status !== "captured" || event.omitted_chars || event.reason))
      status(ctx, "some source content omitted by policy or size limit");
    else status(ctx, "captured");
  });
  pi.on("before_agent_start", async (event, ctx) => {
    if (!matches(ctx) || event.messageType !== "user") return;
    const message = await context(ctx, "prompt", event.prompt);
    if (message) return { message };
  });
  pi.on("context", async (event) => {
    // Replace old automatic context on each provider request. Custom memory is
    // never part of the observed completed-source set.
    const last = event.messages.findLastIndex((message) => message.role === "custom" && message.customType === CUSTOM_TYPE);
    return { messages: event.messages.filter((message, index) =>
      message.role !== "custom" || message.customType !== CUSTOM_TYPE ||
      (index === last && activeContext !== undefined && message.content === activeContext)) };
  });
  pi.on("session_shutdown", async () => { observed.clear(); session = ""; cwd = ""; });
}
