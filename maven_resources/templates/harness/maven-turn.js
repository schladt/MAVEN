import { execFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { isAbsolute, join, resolve, sep } from 'node:path';

const HOME = __MAVEN_HOME_JSON__;
const PYTHON = join(HOME, 'venv', 'bin', 'python');
const FAILURE = 'MAVEN: failed — local turn status could not be verified.';
const OFF = /^turn off maven for this session[.!]*$/i;
const ON = /^turn on maven for this session[.!]*$/i;

function control(prompt) {
  if (typeof prompt !== 'string' || prompt.length > 256) return '';
  const text = prompt.trim().replace(/\s+/g, ' ');
  return OFF.test(text) ? 'off' : ON.test(text) ? 'on' : '';
}

// Only accept literal arguments to a single mounted memory command. A pipeline,
// substitution, redirect or other compound shell program still needs classification.
const MEMORY_COMMAND = /^(?:mcp__(?:maven_)?)?maven_(?:orient|local_read|local_write|turn)(?:[ \t]+(?:'[^'\n]*'|"[^"`$\\\n]*"|[a-zA-Z0-9_@./:=+,-]+))*[ \t]*$/;

function bridge(phase, fields) {
  return new Promise((done) => {
    const child = execFile(PYTHON, ['-I', '-m', 'tools.turn_hook', 'omp', phase, '--home', HOME], {
      cwd: HOME, env: { HOME: process.env.HOME, PATH: '/usr/bin:/bin' },
      timeout: 6000, killSignal: 'SIGKILL', maxBuffer: 1024 * 1024, encoding: 'utf8',
    }, (error, stdout) => {
      if (!error) {
        try { return done(JSON.parse(stdout)); } catch { /* never emit subprocess output */ }
      }
      done(null);
    });
    child.stdin.on('error', () => {}); // A failed spawn/closed pipe is handled above.
    child.stdin.end(JSON.stringify(fields));
  });
}

function target(path, cwd) {
  if (typeof path !== 'string' || !path) return 'opaque';
  path = path.replace(/^\[(.+)#[0-9A-F]{4}\]$/, '$1');
  if (/^(?:agent|proc|local|history):\/\//.test(path)) return null;
  if (/^[\w-]+:\/\//.test(path)) return 'opaque';
  const full = resolve(cwd, path);
  if (full === HOME || full.startsWith(HOME + sep) || full.split(sep).includes('.maven')) return null;
  // SQLite row operations can succeed without changing a row.
  if (/\.(?:sqlite3?|db):/i.test(path)) return 'opaque';
  return 'write';
}

function classify(name, input, cwd) {
  if (name === 'edit' && Array.isArray(input?.paths) && input.paths.length) {
    return input.paths.map(path => target(path, cwd)).find(Boolean) ?? null;
  }
  if (name === 'write' || (name === 'edit' && typeof input?.path === 'string')) {
    return target(input?.path, cwd);
  }
  if (name === 'edit' || name === 'apply_patch') {
    // Recognize structural headers only, not file contents. Unknown formats and
    // mixed-effect operations are left for the agent rather than guessed.
    let seen = false;
    if (typeof input?.input !== 'string') return 'opaque';
    for (const match of input.input.matchAll(/^\[(.+)#[0-9A-F]{4}\]$|^\*\*\* (?:Add File|Update File|Delete File|Edit File|Move to): (.+)$|^MV (.+)$/gm)) {
      seen = true;
      const kind = target((match[1] ?? match[2] ?? match[3]).replace(/^"(.*)"$/, '$1'), cwd);
      if (kind) return kind;
    }
    return seen ? null : 'opaque';
  }
  if (name === 'bash') return MEMORY_COMMAND.test(input?.command ?? '') ? null : 'opaque';
  if (['eval', 'python', 'task', 'resolve', 'lsp'].includes(name)) return 'opaque';
  return null;
}

export default function mavenTurn(pi) {
  // Factories are rebound for each child. Never share observations, conversation
  // IDs, or opt-outs via module state, transcript snapshots or filesystem paths.
  const ephemeralId = randomUUID();
  let turn;
  let views = new Map();
  const calls = new Map();
  const main = ctx => ctx.agent?.kind !== 'sub';
  const identity = ctx => ctx.sessionManager.getSessionId() || ephemeralId;
  const same = ctx => turn?.conversation === identity(ctx) && turn?.cwd === ctx.cwd;
  const fields = t => ({ conversation: t.conversation, cwd: t.cwd, turn_key: t.key });

  function footer(text) {
    pi.sendMessage({ customType: 'maven-turn', content: text, display: true, excludeFromContext: true },
      { triggerTurn: false });
  }

  const digest = text => createHash('sha256').update(text).digest('hex');

  async function settle(t, allowContinuation) {
    if (t.closed) return;
    const result = t.key ? await bridge('end', fields(t)) : null;
    if (!t.failed && result?.request_capture === true && allowContinuation && !t.asked && result.message) {
      t.asked = true;
      t.continuation = result.message;
      return { continue: true, additionalContext: result.message };
    }
    t.closed = true;
    if (t.off || result?.status === 'off') return;
    const verified = !t.failed && result?.footer || FAILURE;
    if (digest(verified) !== t.lastLine) footer(verified);
  }

  // Only the last complete project/Nexus projections are held, in this factory.
  // An opaque view token binds store identity, projection role and revision.
  function projection(r, retained) {
    const context = r.context;
    const next = new Map();
    let expanded = false;
    for (const key of ['active', 'nexus']) {
      const view = context?.[key];
      if (!view || typeof view !== 'object') continue; // Includes "same_as_active".
      if (view.unchanged) {
        const cached = retained.get(view.project_id);
        if (!cached || cached.token !== view.token) return null;
        context[key] = { ...cached, ...view, unchanged: false };
        expanded = true;
      }
      next.set(view.project_id, context[key]);
    }
    views = next;
    if (!expanded) return r.text;
    const newline = r.text.indexOf('\n');
    return newline < 0 ? null : r.text.slice(0, newline + 1) + JSON.stringify(context);
  }

  async function begin(ctx, command = '') {
    const conversation = identity(ctx);
    if (!isAbsolute(ctx.cwd)) return;
    const retained = same(ctx) ? views : new Map();
    views = new Map();
    const t = { conversation, cwd: ctx.cwd, prepared: true, command };
    turn = t;
    calls.clear();
    const request = { conversation, cwd: ctx.cwd, turn_id: randomUUID(), disable: command === 'off', enable: command === 'on' };
    if (!request.disable && retained.size) {
      request.known = Object.fromEntries(Array.from(retained, ([id, v]) => [id, v.token]));
    }
    const r = await bridge('begin', request);
    if (turn !== t) return; // A native session transition superseded this preparation.
    t.off = r?.status === 'off';
    t.key = typeof r?.turn_key === 'string' && r.turn_key || undefined;
    t.text = t.off ? r.text : r && typeof r.text === 'string' ? projection(r, retained) : null;
    t.failed = !t.off && (!t.key || !t.text);
    if (t.failed) t.text = `${FAILURE} Do not assume owner preferences or permission to act.`;
  }

  pi.on('before_agent_start', async (event, ctx) => {
    if (!main(ctx) || event.reason === 'revive') return;
    // OMP starts the stop-hook recovery as a synthetic prompt (reason "prompt"),
    // even for --no-session. Do not rotate the receipt for our own continuation.
    if (same(ctx) && !turn.closed && turn.continuation && event.prompt === turn.continuation) return;
    // A base-system-prompt retry can re-enter preparation before the first
    // provider request. A later dequeued user batch *is* a new boundary.
    const command = control(event.prompt);
    if (same(ctx) && turn.prepared && !turn.closed && turn.command === command) return;
    if (same(ctx) && !turn.closed) {
      if (calls.size && turn.key) {
        const result = await bridge('observe', { ...fields(turn), write: false, opaque: true });
        if (!result) turn.failed = true;
      }
      await settle(turn, false);
    }
    await begin(ctx, command);
  });

  pi.on('agent_start', async (_event, ctx) => {
    // A restored/interrupted agent loop may start without a new prompt.
    if (main(ctx) && !same(ctx)) await begin(ctx);
  });

  pi.on('context', (event, ctx) => {
    if (!main(ctx) || !same(ctx) || turn.closed) return;
    turn.prepared = false;
    // This is an ephemeral provider-context transformation, not a persisted
    // transcript entry. Never feed the original messages to the helper.
    return { messages: [...event.messages, { role: 'custom', customType: 'maven-turn-context',
      content: turn.text, display: false, timestamp: 0 }] };
  });

  pi.on('tool_call', (event, ctx) => {
    if (!main(ctx)) return;
    if (!same(ctx) || turn.prepared || !turn.closed && !turn.off && !turn.text) {
      return { block: true, reason: 'MAVEN turn context was not prepared before this tool. Submit a fresh prompt.' };
    }
    if (turn.off || turn.closed || !turn.key) return;
    const kind = classify(event.toolName, event.input, ctx.cwd);
    if (kind) calls.set(event.toolCallId, { turn, kind });
  });

  pi.on('tool_result', async (event, ctx) => {
    if (!main(ctx)) return;
    const call = calls.get(event.toolCallId);
    calls.delete(event.toolCallId);
    if (!call || call.turn !== turn || !same(ctx) || turn.closed) return;
    if (call.kind === 'write') {
      if (event.details?.errorKind === 'no_change') return;
      if (event.isError) {
        // An aggregate I/O failure may have applied a prefix, never a confirmed batch.
        if (!['io', 'write_failed'].includes(event.details?.errorKind)) return;
        call.kind = 'opaque';
      }
    }
    // Even a failing opaque shell/child operation can have landed a prefix.
    // Never interpret stdout or transmit tool arguments, paths or contents.
    const r = await bridge('observe', { ...fields(call.turn), write: call.kind === 'write', opaque: call.kind === 'opaque' });
    if (!r || r.status === 'failed') call.turn.failed = true;
  });

  pi.on('message_end', ({ message }, ctx) => {
    if (!main(ctx) || !same(ctx) || turn.closed || message?.role !== 'assistant') return;
    const last = message.content?.findLast(block => block.type === 'text')?.text;
    // A fingerprint of a bounded final line, never a transcript or assistant text.
    turn.lastLine = typeof last === 'string' ? digest(last.slice(-1024).trimEnd().split('\n').at(-1)) : null;
  });

  pi.on('session_stop', async (_event, ctx) => {
    if (main(ctx) && same(ctx)) return settle(turn, true);
  });

  for (const event of ['session_start', 'session_clear', 'session_switch', 'session_branch', 'session_tree']) {
    pi.on(event, () => { turn = undefined; views.clear(); calls.clear(); });
  }
}
