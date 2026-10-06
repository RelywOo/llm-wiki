/**
 * WikiInit Prime Agent runtime adapter (v1).
 *
 * Project-local, installed only by `wiki-init --prime`. The durable source of truth
 * stays in `.memory/**` and the command source stays in `.claude/commands/**`.
 * This adapter maps Prime Agent lifecycle events to the existing wiki contour.
 */
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, join, relative } from "node:path";

export const WIKIINIT_PRIME_ADAPTER = 1;

const WIKI_MARK = ".memory/wiki/";
const STATE_MARK = ".memory/STATE.md";
const MAP_MARK = "MAP (";
const HIT_MARK = "STATUS: hit";
const COMPACTION_THRESHOLD = 0.75;
const COMPACTION_INSTRUCTIONS =
  "Create a concise, factual handoff for continuing the same task. Preserve the current goal and " +
  "stage; completed work and verification; decisions and reasons; exact paths, commands and relevant " +
  "results; the next concrete step; open questions and risks; and the latest checkpoint/STATE status. " +
  "Treat .memory/STATE.md and .memory/wiki/ as authoritative. Do not invent facts, claim checks passed " +
  "without evidence, or replace a required /wiki-checkpoint.";
const FALLBACK_COMPACT_ANCHOR = `[reinject after Prime compaction] Context was compacted; details may be lost.
- Volatile state: .memory/STATE.md; durable knowledge: .memory/wiki/.
- Search with: python .memory/tools/structural.py search "<terms>".
- Filed decisions are immutable and superseded forward; log.md is append-only.
- Run /wiki-checkpoint at the first natural pause so surviving details become durable.`;

const CONSULT_GATE_REASON =
  "Memory check gate: this turn declares Memory check: yes without proven project-memory access. Run python .memory/tools/structural.py search \"<terms>\" (or section/toc/active), use the result, and provide only the necessary correction.";
const CONSULT_MAP_REASON =
  "Memory check gate: the 🗺/∅ marker has no memory-map evidence this turn. Run structural.py map or search, then provide only the necessary correction.";
const CONSULT_HIT_REASON =
  "Memory check gate: the 🎯 marker has neither STATUS: hit nor a concrete wiki-page read. Find supporting evidence and provide only the necessary correction.";

const WRITE_MESSAGES = [
  "WRITE-BATCH: пошла запись в память. Все оставшиеся wiki-правки, STATE и log append " +
    "этого checkpoint выполни одним заходом по заранее составленной описи.",
  "WRITE-BATCH СТОП-КРАН: это второй заход записи. Выполни все оставшиеся правки памяти " +
    "сейчас и больше не дроби write-фазу.",
];

function slash(value) {
  return String(value ?? "").replaceAll("\\", "/");
}

function toolCode(event) {
  return event?.toolName === "ipython" ? String(event.input?.code ?? "") : "";
}

function contentText(content) {
  return (content ?? [])
    .filter((block) => block && block.type === "text")
    .map((block) => String(block.text ?? ""))
    .join("\n");
}

function lastAssistantText(messages) {
  for (let i = (messages?.length ?? 0) - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message?.role !== "assistant") continue;
    const blocks = Array.isArray(message.content) ? message.content : [];
    return contentText(blocks);
  }
  return "";
}

function declaration(text) {
  for (const block of String(text).split(/\n\s*\n/)) {
    const first = block.split("\n").find((line) => line.trim());
    const match = first?.trim().match(/^Memory check:\s*(yes|no)(?=\s|$|[·.,;:!?])/iu);
    if (match) return { yes: match[1].toLowerCase() === "yes", line: first.trim() };
  }
  return undefined;
}

function memoryEvidence(code) {
  const normalized = slash(code);
  return normalized.includes("structural.py") || normalized.includes(".memory/wiki") ||
    normalized.includes(STATE_MARK);
}

function concreteHitEvidence(code) {
  const normalized = slash(code);
  return normalized.includes(".memory/wiki/concepts/") ||
    normalized.includes(".memory/wiki/decisions/") ||
    /structural\.py["']?\s*,?\s*["']section\b/.test(normalized) ||
    /structural\.py\s+section\b/.test(normalized);
}

function isMutationCode(code) {
  const normalized = slash(code);
  return /\bedit\s*\(|\.write_(?:text|bytes)\s*\(|\.(?:unlink|rename|replace)\s*\(/.test(normalized) ||
    /(?:^|\W)(?:open|\.open)\s*\([^\n)]*,\s*["'][wax+]/m.test(normalized) ||
    /\bshutil\.(?:copy|copy2|copyfile|move|rmtree)\s*\(/.test(normalized) ||
    /\bos\.(?:remove|unlink|rename|replace)\s*\(/.test(normalized) ||
    /\bsed\s+-i\b|\b(?:rm|mv|cp)\s+-?[^\n]*/.test(normalized) ||
    normalized.includes(">>");
}

function isKnownDestructiveLogWrite(code) {
  const normalized = slash(code);
  return /\.write_(?:text|bytes)\s*\(|\.unlink\s*\(/.test(normalized) ||
    /(?:^|\W)(?:open|\.open)\s*\([^\n)]*,\s*["'][wx]/m.test(normalized) ||
    /\bsed\s+-i\b|\brm\s+-/.test(normalized);
}

function mentionedDecisionPaths(code) {
  const normalized = slash(code);
  const matches = normalized.match(/(?:^|[^\w.-])(\.memory\/wiki\/decisions\/[A-Za-z0-9_.-]+\.md)/g) ?? [];
  return [...new Set(matches.map((match) => {
    const index = match.indexOf(".memory/");
    return match.slice(index);
  }))];
}

function pathHasMutationOnItsStatement(code, path, destructiveOnly = false) {
  const normalized = slash(code);
  return normalized.split("\n").some((line) => line.includes(path) &&
    (destructiveOnly ? isKnownDestructiveLogWrite(line) : isMutationCode(line)));
}

function aliasesForPath(code, path) {
  const aliases = [];
  const assignment = /(?:^|\n)\s*([A-Za-z_]\w*)\s*=\s*(?:Path|pathlib\.Path)\(\s*["']([^"']+)["']\s*\)/g;
  for (const match of slash(code).matchAll(assignment)) {
    if (slash(match[2]) === path) aliases.push(match[1]);
  }
  return [...new Set(aliases)];
}

function aliasHasMutation(code, alias, destructiveOnly = false) {
  const escaped = alias.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const normalized = slash(code);
  const method = destructiveOnly ? "(?:write_text|write_bytes|unlink|rename)" :
    "(?:write_text|write_bytes|unlink|rename)";
  const direct = new RegExp(`\\b${escaped}\\.${method}\\s*\\(`).test(normalized);
  const openMode = destructiveOnly ? "[wx]" : "[wax+]";
  const memberOpen = new RegExp(`\\b${escaped}\\.open\\s*\\(\\s*[\"']${openMode}`).test(normalized);
  const builtinOpen = new RegExp(`\\bopen\\s*\\(\\s*${escaped}\\s*,\\s*[\"']${openMode}`).test(normalized);
  const editCall = !destructiveOnly && new RegExp(`\\bedit\\s*\\([^\n)]*\\bpath\\s*=\\s*${escaped}\\b`).test(normalized);
  return direct || memberOpen || builtinOpen || editCall;
}

function grepNudge(code) {
  const normalized = slash(code);
  if (!/\b(?:grep|egrep|fgrep|rg|ripgrep)\b|select-string/i.test(normalized)) return undefined;
  if (normalized.includes(".memory/wiki")) {
    return "Reminder: for .memory/wiki content lookups prefer `python .memory/tools/structural.py " +
      "search \"<terms>\"` (FTS5/stemming), or section|active for structural queries. " +
      "Literal grep remains fine for exact ids and wikilinks.";
  }
  if (/(?:^|[^\w.-])code\//m.test(normalized)) {
    return "Reminder: for code/ prefer a project-aware semantic/LSP skill when one is installed; " +
      "plain rg remains appropriate for exact literals and when no semantic integration is available.";
  }
  return undefined;
}

function isMemoryWrite(code) {
  const normalized = slash(code);
  return isMutationCode(code) && (normalized.includes(WIKI_MARK) || normalized.includes(STATE_MARK));
}

function appendToolContext(event, messages) {
  if (!messages.length) return undefined;
  return {
    content: [
      ...(event.content ?? []),
      { type: "text", text: messages.join("\n\n") },
    ],
  };
}

function archiveSession(ctx) {
  try {
    const sessionFile = ctx.sessionManager.getSessionFile();
    if (!sessionFile || !existsSync(sessionFile)) return;
    const archiveDir = join(dirname(sessionFile), "pre-compact-archive");
    mkdirSync(archiveDir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 17);
    const sessionId = ctx.sessionManager.getSessionId?.() || basename(sessionFile, ".jsonl");
    copyFileSync(sessionFile, join(archiveDir, `${sessionId}.${stamp}.jsonl`));
  } catch {
    // Fail-open: compaction must never be bricked by archival trouble.
  }
}

function collectMemoryFiles(root) {
  const files = new Map();
  const walk = (dir) => {
    if (!existsSync(dir)) return;
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.isFile() && entry.name.endsWith(".md")) {
        files.set(slash(relative(root, path)), readFileSync(path));
      }
    }
  };
  walk(join(root, ".memory", "wiki"));
  const state = join(root, STATE_MARK);
  if (existsSync(state)) files.set(STATE_MARK, readFileSync(state));
  return files;
}

function captureMemory(root, trackedDecisions) {
  return { root, files: collectMemoryFiles(root), trackedDecisions: new Set(trackedDecisions) };
}

function changedMemoryPaths(before, after) {
  const paths = new Set([...before.keys(), ...after.keys()]);
  return [...paths].filter((path) => {
    const oldValue = before.get(path);
    const newValue = after.get(path);
    return !oldValue || !newValue || !oldValue.equals(newValue);
  }).sort();
}

function restoreMemory(snapshot) {
  const current = collectMemoryFiles(snapshot.root);
  for (const path of current.keys()) {
    if (!snapshot.files.has(path)) rmSync(join(snapshot.root, path), { force: true });
  }
  for (const [path, content] of snapshot.files) {
    const target = join(snapshot.root, path);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, content);
  }
}

function normalizedText(buffer) {
  return buffer.toString("utf8").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

function validateMemoryTransaction(snapshot) {
  const after = collectMemoryFiles(snapshot.root);
  const violations = [];
  for (const path of snapshot.trackedDecisions) {
    const before = snapshot.files.get(path);
    if (!before) continue;
    const current = after.get(path);
    if (!current || !before.equals(current)) {
      violations.push(
        `Iron rule: filed decision ${path} is immutable. The complete wiki/STATE write-set ` +
        "from this IPython call was rolled back; supersede forward instead.",
      );
    }
  }
  const logPath = ".memory/wiki/log.md";
  const beforeLog = snapshot.files.get(logPath);
  if (beforeLog) {
    const currentLog = after.get(logPath);
    if (!currentLog || !normalizedText(currentLog).startsWith(normalizedText(beforeLog))) {
      violations.push(
        "Iron rule: log.md is append-only. The complete wiki/STATE write-set from this " +
        "IPython call was rolled back; append only at EOF.",
      );
    }
  }
  if (violations.length) {
    restoreMemory(snapshot);
    return { violations, changedPaths: [], rolledBack: true };
  }
  return {
    violations: [],
    changedPaths: changedMemoryPaths(snapshot.files, after),
    rolledBack: false,
  };
}

function sessionKey(ctx) {
  const manager = ctx?.sessionManager;
  try {
    const id = manager?.getSessionId?.();
    if (id) return String(id);
  } catch {
    // A degraded id accessor must not skip the still-usable file identity.
  }
  try {
    const file = manager?.getSessionFile?.();
    if (file) return String(file);
  } catch {
    // Fall through to the fail-open compatibility bucket.
  }
  return "__default__";
}

function freshSessionState(deferredMessages = []) {
  return {
    pendingNudges: new Map(),
    evidence: false,
    mapSeen: false,
    hitSeen: false,
    currentTurn: 0,
    writeTurns: new Set(),
    gateBlockedOnce: false,
    resetPending: false,
    compactionPending: false,
    compactionInFlight: false,
    postCompactAnchor: undefined,
    pendingSnapshots: new Map(),
    deferredMessages: [...deferredMessages],
  };
}


export default function wikiMemoryPrime(pi) {
  // One ResourceLoader can serve a parent and several RLM children concurrently.  Extension
  // handler identity is shared, so every turn-scoped bit must live under the native session id.
  const sessions = new Map();
  const mutationLocks = new Map();
  const stateFor = (ctx) => {
    const key = sessionKey(ctx);
    if (!sessions.has(key)) sessions.set(key, freshSessionState());
    return sessions.get(key);
  };
  const releaseStateLocks = (state) => {
    for (const pending of state.pendingSnapshots.values()) {
      if (pending.locked && mutationLocks.get(pending.repoKey) === pending.owner) {
        mutationLocks.delete(pending.repoKey);
      }
    }
    state.pendingSnapshots.clear();
  };

  pi.on("resources_discover", (event) => {
    const commands = ["wiki-resume", "wiki-checkpoint", "wiki-clear"]
      .map((name) => join(event.cwd, existsSync(join(event.cwd, ".memory", "runtimes.json")) ? ".memory" : ".claude", "commands", `${name}.md`))
      .filter((path) => existsSync(path));
    return commands.length ? { promptPaths: commands } : undefined;
  });

  pi.on("input", (_event, ctx) => {
    const key = sessionKey(ctx);
    const state = stateFor(ctx);
    let idle = true;
    try {
      if (typeof ctx?.isIdle === "function") idle = Boolean(ctx.isIdle());
    } catch {
      // Treat an unavailable idle probe as an immediately admitted input.
    }
    if (idle) sessions.set(key, freshSessionState(state.deferredMessages));
    else state.resetPending = true; // queued input: do not erase the still-running turn
    return { action: "continue" };
  });

  pi.on("before_agent_start", async (_event, ctx) => {
    const key = sessionKey(ctx);
    let state = stateFor(ctx);
    if (state.resetPending) {
      sessions.set(key, freshSessionState(state.deferredMessages));
      state = stateFor(ctx);
    }
    const messages = state.deferredMessages.splice(0);
    if (state.postCompactAnchor) {
      messages.push(state.postCompactAnchor);
      state.postCompactAnchor = undefined;
    }
    const cwd = ctx?.cwd;
    const script = cwd && join(cwd, ".memory", "tools", "consult_nudge.py");
    if (script && existsSync(script)) {
      try {
        const result = await pi.exec("python", [script], { cwd, timeout: 10_000 });
        if (result.code === 0 && result.stdout.trim()) messages.push(result.stdout.trim());
      } catch {
        // Fail-open, same policy as the Claude hook.
      }
    }
    if (messages.length) {
      return {
        message: {
          customType: "wiki-session-directives",
          content: messages.join("\n\n"),
          display: false,
        },
      };
    }
  });

  pi.on("turn_start", (event, ctx) => {
    stateFor(ctx).currentTurn = event.turnIndex;
  });

  pi.on("context", (event, ctx) => {
    const state = stateFor(ctx);
    if (!state.postCompactAnchor) return;
    const anchor = state.postCompactAnchor;
    state.postCompactAnchor = undefined;
    return {
      messages: [
        ...event.messages,
        {
          role: "custom",
          customType: "wiki-postcompact-anchor",
          content: anchor,
          display: false,
          timestamp: Date.now(),
        },
      ],
    };
  });

  pi.on("tool_call", async (event, ctx) => {
    const code = toolCode(event);
    if (!code) return;
    const state = stateFor(ctx);

    if (memoryEvidence(code)) state.evidence = true;
    if (concreteHitEvidence(code)) state.hitSeen = true;

    const nudge = grepNudge(code);
    if (nudge) state.pendingNudges.set(event.toolCallId, nudge);

    const mutation = isMutationCode(code);
    if (mutation) {
      const logPath = ".memory/wiki/log.md";
      const destructiveLogAlias = aliasesForPath(code, logPath)
        .some((alias) => aliasHasMutation(code, alias, true));
      if (pathHasMutationOnItsStatement(code, logPath, true) || destructiveLogAlias) {
        return {
          block: true,
          reason: "Iron rule: log.md is append-only. Use a pure append; never rewrite, truncate, move, or delete it.",
        };
      }

      for (const path of mentionedDecisionPaths(code)) {
        const aliasMutation = aliasesForPath(code, path)
          .some((alias) => aliasHasMutation(code, alias, false));
        if (!pathHasMutationOnItsStatement(code, path) && !aliasMutation) continue;
        try {
          const tracked = await pi.exec("git", ["ls-files", "--error-unmatch", "--", path], {
            cwd: ctx.cwd,
            timeout: 10_000,
          });
          if (tracked.code === 0) {
            return {
              block: true,
              reason: `Iron rule: filed decision ${path} is immutable. Create a new decision with supersedes instead.`,
            };
          }
        } catch {
          // The transactional post-check below is authoritative for non-literal paths.
        }
      }
    }

    const repoKey = slash(ctx.cwd);
    const owner = `${sessionKey(ctx)}:${event.toolCallId}`;
    if (mutation) {
      const active = mutationLocks.get(repoKey);
      if (active && active !== owner) {
        return {
          block: true,
          reason: "Prime wiki transaction guard: another filesystem mutation is still running in " +
            "this repository. Retry this IPython call after that tool result; parallel reads remain allowed.",
        };
      }
      mutationLocks.set(repoKey, owner);
    }

    try {
      const tracked = await pi.exec(
        "git",
        ["ls-files", "-z", "--", ".memory/wiki/decisions/*.md"],
        { cwd: ctx.cwd, timeout: 10_000 },
      );
      if (tracked.code !== 0) throw new Error(tracked.stderr || "git ls-files failed");
      const trackedDecisions = tracked.stdout.split("\0").filter(Boolean).map(slash);
      state.pendingSnapshots.set(event.toolCallId, {
        snapshot: captureMemory(ctx.cwd, trackedDecisions),
        repoKey,
        owner,
        locked: mutation,
      });
    } catch (error) {
      if (mutation && mutationLocks.get(repoKey) === owner) mutationLocks.delete(repoKey);
      if (mutation) {
        return {
          block: true,
          reason: `Prime wiki transaction guard could not capture a safe baseline: ${error}.`,
        };
      }
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    const state = stateFor(ctx);
    const output = contentText(event.content);
    if (output.includes(MAP_MARK)) state.mapSeen = true;
    if (output.includes(HIT_MARK)) state.hitSeen = true;

    const messages = [];
    const nudge = state.pendingNudges.get(event.toolCallId);
    if (nudge) {
      messages.push(nudge);
      state.pendingNudges.delete(event.toolCallId);
    }

    const pending = state.pendingSnapshots.get(event.toolCallId);
    state.pendingSnapshots.delete(event.toolCallId);
    let validation;
    if (pending) {
      try {
        validation = validateMemoryTransaction(pending.snapshot);
      } catch (error) {
        try { restoreMemory(pending.snapshot); } catch { /* structural guard remains the floor */ }
        if (pending.locked && mutationLocks.get(pending.repoKey) === pending.owner) {
          mutationLocks.delete(pending.repoKey);
        }
        const patch = appendToolContext(event, [
          ...messages,
          `Prime wiki transaction POST-CHECK FAILED and attempted rollback: ${error}. ` +
            "Run `python .memory/tools/structural.py guard` before any further memory write.",
        ]);
        return { ...(patch ?? {}), isError: true };
      }
      if (pending.locked && mutationLocks.get(pending.repoKey) === pending.owner) {
        mutationLocks.delete(pending.repoKey);
      }
      if (validation.rolledBack) {
        const patch = appendToolContext(event, [...messages, ...validation.violations]);
        return { ...(patch ?? {}), isError: true };
      }
    }

    const code = String(event.input?.code ?? "");
    const memoryChanged = validation?.changedPaths?.some(
      (path) => path === STATE_MARK || path.startsWith(WIKI_MARK),
    );
    if (memoryChanged || (!validation && isMemoryWrite(code))) {
      const turn = state.currentTurn;
      if (!state.writeTurns.has(turn)) {
        state.writeTurns.add(turn);
        const round = state.writeTurns.size;
        if (round <= WRITE_MESSAGES.length) messages.push(WRITE_MESSAGES[round - 1]);
        else messages.push(
          `WRITE-BATCH: превышен потолок двух заходов записи (заход №${round}). ` +
          "Выполни все оставшиеся правки сейчас и больше не возвращайся к write-фазе.",
        );
      }
    }
    return appendToolContext(event, messages);
  });

  pi.on("turn_end", (_event, ctx) => {
    const state = stateFor(ctx);
    if (state.compactionInFlight) return;
    let usage;
    try {
      usage = ctx?.getContextUsage?.();
    } catch {
      return;
    }
    if (!usage) return;
    const percent = Number.isFinite(Number(usage.percent))
      ? Number(usage.percent)
      : Number(usage.tokens || 0) / Number(usage.contextWindow || 0) * 100;
    if (Number.isFinite(percent) && percent >= COMPACTION_THRESHOLD * 100) {
      state.compactionPending = true;
    }
  });

  pi.on("session_before_compact", (_event, ctx) => {
    const state = stateFor(ctx);
    state.compactionPending = false;
    state.compactionInFlight = true;
    archiveSession(ctx);
  });

  pi.on("session_compact", async (_event, ctx) => {
    const state = stateFor(ctx);
    state.compactionPending = false;
    state.compactionInFlight = false;
    let anchor = FALLBACK_COMPACT_ANCHOR;
    const script = join(ctx.cwd, ".memory", "tools", "compact_anchor.py");
    if (existsSync(script)) {
      try {
        const result = await pi.exec("python", [script], { cwd: ctx.cwd, timeout: 10_000 });
        if (result.code === 0 && result.stdout.trim()) anchor = result.stdout.trim();
      } catch {
        // The complete fallback still reaches the immediate post-compact context event.
      }
    }
    state.postCompactAnchor = anchor;
  });

  pi.on("session_shutdown", (_event, ctx) => {
    const state = stateFor(ctx);
    releaseStateLocks(state);
    sessions.delete(sessionKey(ctx));
  });

  pi.on("agent_end", (event, ctx) => {
    const state = stateFor(ctx);
    releaseStateLocks(state);

    const decl = declaration(lastAssistantText(event.messages));
    if (decl?.yes && !state.gateBlockedOnce) {
      let reason;
      if (!state.evidence) reason = CONSULT_GATE_REASON;
      else if (/[🗺∅]/u.test(decl.line) && !state.mapSeen) reason = CONSULT_MAP_REASON;
      else if (decl.line.includes("🎯") && !state.hitSeen) reason = CONSULT_HIT_REASON;
      if (reason) {
        state.gateBlockedOnce = true;
        state.deferredMessages.push(reason);
        // Prime 0.8 has no session-bound triggerTurn action. Never use shared pi.sendMessage:
        // it can cross-route from a parent into the last-bound RLM child.
        try {
          if (ctx?.hasUI) ctx.ui.notify(reason, "warning");
        } catch {
          // Headless/RLM sessions receive the delayed correction on their next turn.
        }
      }
    }

    if (!state.compactionPending || state.compactionInFlight) return;
    state.compactionInFlight = true;
    setTimeout(() => {
      // `turn_end` is still inside an active multi-tool run. Compact only after agent_end
      // has returned and this exact session state is idle; otherwise retry next agent_end.
      if (stateFor(ctx) !== state || (typeof ctx?.isIdle === "function" && !ctx.isIdle())) {
        state.compactionInFlight = false;
        return;
      }
      try {
        ctx.compact({
          customInstructions: COMPACTION_INSTRUCTIONS,
          onComplete: () => {
            state.compactionPending = false;
            state.compactionInFlight = false;
          },
          onError: (error) => {
            state.compactionInFlight = false;
            try {
              if (ctx?.hasUI) ctx.ui.notify(
                `Prime wiki proactive compaction failed: ${error?.message || error}`,
                "warning",
              );
            } catch {
              // Headless/RLM sessions retry at the next agent boundary.
            }
          },
        });
      } catch (error) {
        state.compactionInFlight = false;
        try {
          if (ctx?.hasUI) ctx.ui.notify(
            `Prime wiki proactive compaction failed: ${error?.message || error}`,
            "warning",
          );
        } catch {
          // Built-in auto-compaction remains the final fallback.
        }
      }
    }, 0);
  });
}
