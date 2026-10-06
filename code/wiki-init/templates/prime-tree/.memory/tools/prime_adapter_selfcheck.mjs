#!/usr/bin/env node
/** Deterministic self-check for the WikiInit Prime Agent adapter. */
const WIKIINIT_PRIME_SELFCHECK = 1;
import assert from "node:assert/strict";
import {
  existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const root = fileURLToPath(new URL("../../", import.meta.url));
const adapterUrl = new URL("../../.prime/agent/extensions/wiki-memory.js", import.meta.url);
assert.ok(existsSync(fileURLToPath(adapterUrl)), "Prime adapter exists");
const { default: adapter } = await import(`${adapterUrl.href}?selfcheck=${Date.now()}`);

const handlers = new Map();
const sent = [];
const pi = {
  on(name, fn) {
    if (!handlers.has(name)) handlers.set(name, []);
    handlers.get(name).push(fn);
  },
  async exec(command, args) {
    if (command === "git") {
      if (args.includes("-z")) {
        return { code: 0, stdout: ".memory/wiki/decisions/0001-filed.md\0", stderr: "" };
      }
      const filed = args.some((arg) => String(arg).includes("0001-filed.md"));
      return { code: filed ? 0 : 1, stdout: "", stderr: "" };
    }
    if (String(args[0]).endsWith("consult_nudge.py")) {
      return { code: 0, stdout: "CONSULT DIRECTIVE\n", stderr: "" };
    }
    if (String(args[0]).endsWith("compact_anchor.py")) {
      return { code: 0, stdout: "COMPACT ANCHOR\n", stderr: "" };
    }
    return { code: 0, stdout: "", stderr: "" };
  },
  sendMessage(message, options) { sent.push({ message, options }); },
};
adapter(pi);
const one = (name) => {
  const list = handlers.get(name) ?? [];
  assert.equal(list.length, 1, `${name} registered once`);
  return list[0];
};
const sessionCtx = (id, options = {}) => ({
  cwd: root,
  isIdle: () => options.idle ?? true,
  hasUI: false,
  sessionManager: {
    getSessionId: () => {
      if (options.throwId) throw new Error("id unavailable");
      return id;
    },
    getSessionFile: () => options.file ?? join(root, `${id}.jsonl`),
  },
});

const resources = one("resources_discover")({ cwd: root, reason: "startup" });
assert.deepEqual(resources.promptPaths.map((path) => path.split(/[\\/]/).at(-1)).sort(),
  ["wiki-checkpoint.md", "wiki-clear.md", "wiki-resume.md"], "native /wiki-* sources exposed");

const nudge = await one("before_agent_start")({}, { cwd: root });
assert.equal(nudge.message.customType, "wiki-session-directives", "consult directive injected");

await one("input")();
let result = await one("tool_call")({
  toolName: "ipython", toolCallId: "mixed",
  input: { code: `concept = Path(".memory/wiki/concepts/x.md")
concept.write_text("x")
with open(".memory/wiki/log.md", "a") as f: f.write("ok")` },
}, { cwd: root });
assert.equal(result, undefined, "legal mixed checkpoint batch not falsely blocked");
await one("tool_result")({
  toolCallId: "mixed", input: { code: "" }, content: [{ type: "text", text: "ok" }],
}, { cwd: root });

result = await one("tool_call")({
  toolName: "ipython", toolCallId: "log",
  input: { code: `Path(".memory/wiki/log.md").write_text("bad")` },
}, { cwd: root });
assert.equal(result.block, true, "destructive log rewrite blocked");

result = await one("tool_call")({
  toolName: "ipython", toolCallId: "log-alias",
  input: { code: `log = Path(".memory/wiki/log.md")
log.write_text("bad")` },
}, { cwd: root });
assert.equal(result.block, true, "destructive log alias rewrite blocked");

result = await one("tool_call")({
  toolName: "ipython", toolCallId: "filed",
  input: { code: `await edit(path=".memory/wiki/decisions/0001-filed.md", old_str="a", new_str="b")` },
}, { cwd: root });
assert.equal(result.block, true, "filed decision mutation blocked");

result = await one("tool_call")({
  toolName: "ipython", toolCallId: "filed-alias",
  input: { code: `decision = Path(".memory/wiki/decisions/0001-filed.md")
decision.unlink()` },
}, { cwd: root });
assert.equal(result.block, true, "filed decision alias mutation blocked");

result = await one("tool_call")({
  toolName: "ipython", toolCallId: "new",
  input: { code: `await edit(path=".memory/wiki/decisions/0002-new.md", old_str="a", new_str="b")` },
}, { cwd: root });
assert.equal(result, undefined, "new decision remains editable");
await one("tool_result")({
  toolCallId: "new", input: { code: "" }, content: [{ type: "text", text: "ok" }],
}, { cwd: root });

const txnRoot = mkdtempSync(join(tmpdir(), "wiki-prime-transaction-"));
try {
  const decisionDir = join(txnRoot, ".memory/wiki/decisions");
  const conceptDir = join(txnRoot, ".memory/wiki/concepts");
  mkdirSync(decisionDir, { recursive: true });
  mkdirSync(conceptDir, { recursive: true });
  const filedPath = join(decisionDir, "0001-filed.md");
  const conceptPath = join(conceptDir, "x.md");
  const logPath = join(txnRoot, ".memory/wiki/log.md");
  writeFileSync(filedPath, "filed\n");
  writeFileSync(conceptPath, "living\n");
  writeFileSync(logPath, "log\n");
  writeFileSync(join(txnRoot, ".memory/STATE.md"), "state\n");
  const ctxTxnA = { ...sessionCtx("txn-A"), cwd: txnRoot };
  const ctxTxnB = { ...sessionCtx("txn-B"), cwd: txnRoot };
  await one("input")({}, ctxTxnA);
  await one("input")({}, ctxTxnB);

  const dynamicDecision = `p = ".memory/wiki/decisions/0001-filed.md"\nPath(p).write_text("bad")`;
  result = await one("tool_call")({
    toolName: "ipython", toolCallId: "dynamic", input: { code: dynamicDecision },
  }, ctxTxnA);
  assert.equal(result, undefined, "dynamic decision path reaches transactional post-check");
  writeFileSync(filedPath, "bad\n");
  writeFileSync(conceptPath, "partial\n");
  result = await one("tool_result")({
    toolCallId: "dynamic", input: { code: dynamicDecision },
    content: [{ type: "text", text: "ran" }],
  }, ctxTxnA);
  assert.equal(result.isError, true, "dynamic filed-decision mutation is rejected after execution");
  assert.equal(readFileSync(filedPath, "utf8"), "filed\n", "filed decision restored");
  assert.equal(readFileSync(conceptPath, "utf8"), "living\n", "whole wiki transaction restored");

  const dynamicLog = `src = "/tmp/x"\nshutil.copyfile(src, ".memory/wiki/log.md")`;
  result = await one("tool_call")({
    toolName: "ipython", toolCallId: "shutil-log", input: { code: dynamicLog },
  }, ctxTxnA);
  assert.equal(result, undefined, "shutil log rewrite reaches transactional post-check");
  writeFileSync(logPath, "rewritten\n");
  result = await one("tool_result")({
    toolCallId: "shutil-log", input: { code: dynamicLog },
    content: [{ type: "text", text: "ran" }],
  }, ctxTxnA);
  assert.equal(result.isError, true, "shutil log rewrite is rolled back");
  assert.equal(readFileSync(logPath, "utf8"), "log\n", "append-only log restored");

  result = await one("tool_call")({
    toolName: "ipython", toolCallId: "same-txn", input: { code: dynamicDecision },
  }, ctxTxnA);
  assert.equal(result, undefined, "A owns the mutation transaction");
  const blockedConcurrent = await one("tool_call")({
    toolName: "ipython", toolCallId: "same-txn", input: { code: dynamicDecision },
  }, ctxTxnB);
  assert.equal(blockedConcurrent.block, true, "same-id concurrent RLM mutation cannot collide");
  await one("tool_result")({
    toolCallId: "same-txn", input: { code: dynamicDecision },
    content: [{ type: "text", text: "A done" }],
  }, ctxTxnA);
  result = await one("tool_call")({
    toolName: "ipython", toolCallId: "same-txn", input: { code: dynamicDecision },
  }, ctxTxnB);
  assert.equal(result, undefined, "B may retry after A releases the repository mutation lock");
  await one("tool_result")({
    toolCallId: "same-txn", input: { code: dynamicDecision },
    content: [{ type: "text", text: "B done" }],
  }, ctxTxnB);
} finally {
  rmSync(txnRoot, { recursive: true, force: true });
}

await one("input")();
await one("tool_call")({
  toolName: "ipython", toolCallId: "grep",
  input: { code: `%%bash
rg foo .memory/wiki` },
}, { cwd: root });
result = await one("tool_result")({
  toolCallId: "grep", input: { code: `%%bash
rg foo .memory/wiki` },
  content: [{ type: "text", text: "result" }],
});
assert.match(result.content.at(-1).text, /structural\.py/, "grep nudge reaches model");

await one("input")();
sent.length = 0;
one("agent_end")({ messages: [{
  role: "assistant", content: [{ type: "text", text: "Memory check: yes · ∅\nanswer" }],
}] });
await one("input")(); // the next admitted user turn must preserve the deferred correction
const correction = await one("before_agent_start")({}, { cwd: root });
assert.match(correction.message.content, /Memory check gate/, "English declaration defers a correction");
assert.equal(sent.length, 0, "session-bound correction never uses shared pi.sendMessage");

await one("input")();
await one("tool_call")({
  toolName: "ipython", toolCallId: "search",
  input: { code: `python .memory/tools/structural.py search "x"` },
}, { cwd: root });
await one("tool_result")({
  toolCallId: "search", input: { code: "" },
  content: [{ type: "text", text: "MAP (all)" }],
});
one("agent_end")({ messages: [{
  role: "assistant", content: [{ type: "text", text: "Memory check: yes · ∅\nnot in wiki" }],
}] });
const validNext = await one("before_agent_start")({}, { cwd: root });
assert.doesNotMatch(validNext.message.content, /Memory check gate/, "map evidence satisfies ∅ declaration");

// One ResourceLoader shares this handler identity across parent/RLM children. Interleave two
// native session ids and prove that evidence, pending nudges, write rounds and gate state do not leak.
const ctxA = sessionCtx("session-A");
const ctxB = sessionCtx("session-B");
await one("input")({}, ctxA);
await one("input")({}, ctxB);
await one("tool_call")({
  toolName: "ipython", toolCallId: "search-A",
  input: { code: `python .memory/tools/structural.py search "x"` },
}, ctxA);
await one("tool_result")({
  toolCallId: "search-A", input: { code: "" },
  content: [{ type: "text", text: "MAP (all)" }],
}, ctxA);
sent.length = 0;
one("agent_end")({ messages: [{
  role: "assistant", content: [{ type: "text", text: "Memory check: yes · ∅\nB" }],
}] }, ctxB);
one("agent_end")({ messages: [{
  role: "assistant", content: [{ type: "text", text: "Memory check: yes · ∅\nA" }],
}] }, ctxA);
const nextA = await one("before_agent_start")({}, ctxA);
const nextB = await one("before_agent_start")({}, ctxB);
assert.doesNotMatch(nextA.message.content, /Memory check gate/, "A keeps its own valid evidence");
assert.match(nextB.message.content, /Memory check gate/, "B cannot reuse A evidence");
assert.equal(sent.length, 0, "interleaved sessions never call shared sendMessage");

const queueOptions = { idle: true };
const ctxQueued = sessionCtx("queued", queueOptions);
await one("input")({}, ctxQueued);
await one("tool_call")({
  toolName: "ipython", toolCallId: "queued-search",
  input: { code: `python .memory/tools/structural.py search "x"` },
}, ctxQueued);
await one("tool_result")({
  toolCallId: "queued-search", input: { code: "" },
  content: [{ type: "text", text: "MAP (all)" }],
}, ctxQueued);
queueOptions.idle = false;
await one("input")({}, ctxQueued); // admitted to queue while the current turn still runs
one("agent_end")({ messages: [{
  role: "assistant", content: [{ type: "text", text: "Memory check: yes · ∅\ncurrent turn" }],
}] }, ctxQueued);
queueOptions.idle = true;
const queuedNext = await one("before_agent_start")({}, ctxQueued);
assert.doesNotMatch(queuedNext.message.content, /Memory check gate/,
  "queued input does not erase current-turn evidence before agent_end");

const ctxFileA = sessionCtx("ignored-A", { throwId: true, file: "/tmp/session-file-A.jsonl" });
const ctxFileB = sessionCtx("ignored-B", { throwId: true, file: "/tmp/session-file-B.jsonl" });
await one("input")({}, ctxFileA);
await one("input")({}, ctxFileB);
await one("tool_call")({
  toolName: "ipython", toolCallId: "file-search",
  input: { code: `python .memory/tools/structural.py search "x"` },
}, ctxFileA);
await one("tool_result")({
  toolCallId: "file-search", input: { code: "" }, content: [{ type: "text", text: "MAP (all)" }],
}, ctxFileA);
one("agent_end")({ messages: [{
  role: "assistant", content: [{ type: "text", text: "Memory check: yes · ∅\nfile B" }],
}] }, ctxFileB);
const fileBNext = await one("before_agent_start")({}, ctxFileB);
assert.match(fileBNext.message.content, /Memory check gate/,
  "throwing id accessor falls back to distinct session file");

await one("input")({}, ctxA);
await one("input")({}, ctxB);
const memoryWriteResult = (id, ctx) => one("tool_result")({
  toolCallId: id,
  input: { code: `Path(".memory/STATE.md").write_text("x")` },
  content: [{ type: "text", text: "ok" }],
}, ctx);
one("turn_start")({ turnIndex: 1 }, ctxA);
one("turn_start")({ turnIndex: 1 }, ctxB);
const aRound1 = await memoryWriteResult("a-write-1", ctxA);
const aSameTurn = await memoryWriteResult("a-write-parallel", ctxA);
one("turn_start")({ turnIndex: 2 }, ctxA);
const aRound2 = await memoryWriteResult("a-write-2", ctxA);
const bRound1 = await memoryWriteResult("b-write-1", ctxB);
assert.match(aRound1.content.at(-1).text, /пошла запись/, "A starts at write round one");
assert.equal(aSameTurn, undefined, "parallel writes in one assistant turn count as one round");
assert.match(aRound2.content.at(-1).text, /второй заход/i, "A advances on a distinct turn");
assert.match(bRound1.content.at(-1).text, /пошла запись/, "B starts at its own round one");

await one("input")({}, ctxA);
await one("input")({}, ctxB);
await one("tool_call")({
  toolName: "ipython", toolCallId: "same-id", input: { code: `%%bash\nrg foo .memory/wiki` },
}, ctxA);
const bNoLeak = await one("tool_result")({
  toolCallId: "same-id", input: { code: "" }, content: [{ type: "text", text: "B" }],
}, ctxB);
assert.equal(bNoLeak, undefined, "pending grep nudge does not leak to B");
const aGetsOwn = await one("tool_result")({
  toolCallId: "same-id", input: { code: "" }, content: [{ type: "text", text: "A" }],
}, ctxA);
assert.match(aGetsOwn.content.at(-1).text, /structural\.py/, "A receives its own pending nudge");
await one("tool_call")({
  toolName: "ipython", toolCallId: "cleanup-search",
  input: { code: `python .memory/tools/structural.py search "x"` },
}, ctxA);
await one("tool_result")({
  toolCallId: "cleanup-search", input: { code: "" }, content: [{ type: "text", text: "MAP (all)" }],
}, ctxA);
one("session_shutdown")({}, ctxA);
one("agent_end")({ messages: [{
  role: "assistant", content: [{ type: "text", text: "Memory check: yes · ∅\nrecreated A" }],
}] }, ctxA);
const recreatedA = await one("before_agent_start")({}, ctxA);
assert.match(recreatedA.message.content, /Memory check gate/, "shutdown evicts A state before recreation");

const compactCalls = [];
const ctxThreshold = {
  ...sessionCtx("threshold"),
  getContextUsage: () => ({ tokens: 751, contextWindow: 1000, percent: 75.1 }),
  compact: (options) => compactCalls.push(options),
};
one("turn_end")({}, ctxThreshold);
one("turn_end")({}, ctxThreshold);
assert.equal(compactCalls.length, 0, "threshold never aborts a still-running tool loop");
one("agent_end")({ messages: [{ role: "assistant", content: [] }] }, ctxThreshold);
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(compactCalls.length, 1, "75% threshold compacts once after agent_end becomes idle");
assert.match(compactCalls[0].customInstructions, /\.memory\/STATE\.md/, "wiki handoff instructions supplied");
compactCalls[0].onComplete();
const ctxBelow = {
  ...ctxThreshold,
  getContextUsage: () => ({ tokens: 749, contextWindow: 1000, percent: 74.9 }),
};
one("turn_end")({}, ctxBelow);
one("agent_end")({ messages: [{ role: "assistant", content: [] }] }, ctxBelow);
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(compactCalls.length, 1, "below-threshold turn does not compact");

const temp = mkdtempSync(join(tmpdir(), "wiki-prime-adapter-"));
try {
  const session = join(temp, "session.jsonl");
  writeFileSync(session, "{\"x\":1}\n");
  one("session_before_compact")({}, {
    sessionManager: { getSessionFile: () => session, getSessionId: () => "sid" },
  });
  const archiveDir = join(temp, "pre-compact-archive");
  assert.ok(existsSync(archiveDir) && readdirSync(archiveDir).length === 1,
    "precompact transcript archived");
} finally {
  rmSync(temp, { recursive: true, force: true });
}

sent.length = 0;
const ctxCompact = sessionCtx("compact-session");
await one("session_compact")({}, ctxCompact);
const compactContext = one("context")({ messages: [] }, ctxCompact);
assert.match(compactContext.messages.at(-1).content, /COMPACT ANCHOR/,
  "postcompact anchor reaches the immediate continuation provider call");
assert.equal(one("context")({ messages: [] }, ctxCompact), undefined,
  "postcompact anchor is injected exactly once");
assert.equal(sent.length, 0, "compact anchor never uses shared sendMessage");

const source = readFileSync(fileURLToPath(adapterUrl), "utf8");
assert.doesNotMatch(source, /\bpi\.sendMessage\s*\(/, "shared runtime sender is never called");
assert.match(source, /WIKIINIT_PRIME_ADAPTER\s*=\s*1/, "adapter marker stable");
console.log("prime adapter selfcheck OK");
