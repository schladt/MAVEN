// Exercise the installed factory through only public OMP extension contracts.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [extension, home, cwd] = process.argv.slice(2);
const { default: bind } = await import(pathToFileURL(extension));
const events = () => readFileSync(`${home}/calls.jsonl`, 'utf8').trim().split('\n').map(JSON.parse);
const footers = [];
function harness() {
  const handlers = new Map();
  bind({ on: (name, fn) => handlers.set(name, fn), sendMessage: (m, o) => {
    assert.equal(m.excludeFromContext, true); assert.equal(m.display, true);
    assert.equal(o.triggerTurn, false); footers.push(m.content);
  } });
  return async (name, event, ctx) => handlers.get(name)?.(event, ctx);
}
const ctx = { cwd, agent: { kind: 'main' }, sessionManager: { getSessionId: () => 'native-conversation' } };
const child = { ...ctx, agent: {kind: 'sub'} };
const prompt = 'Write a commit message only. PRIVATE_PROMPT';
let emit = harness();
await emit('session_start', {}, ctx);
await emit('before_agent_start', { prompt, reason: 'prompt' }, child);
await emit('before_agent_start', { prompt, reason: 'revive' }, child);
assert.equal((await emit('tool_call', { toolName: 'write', input: {} }, ctx)).block, true);
await emit('before_agent_start', { prompt, reason: 'prompt' }, ctx);
await emit('before_agent_start', { prompt, reason: 'prompt' }, ctx); // OMP base-policy retry
await emit('agent_start', {}, ctx);
const provider = (await emit('context', { messages: [{role: 'user', content: 'PRIVATE_MESSAGE'}] }, ctx)).messages;
assert.match(provider.at(-1).content, /manual-commit preference/);
assert.equal(events().length, 1);
let id = 0;
async function tool(name, input, isError = false, context = ctx) {
  const call = { toolCallId: String(++id), toolName: name, input };
  assert.equal(await emit('tool_call', call, context), undefined);
  await emit('tool_result', {...call, isError, details: {unrelated: 'PRIVATE_OUTPUT'}, content: [{ type: 'text', text: 'PRIVATE_OUTPUT' }]}, context);
}
await tool('write', {path: 'first.js', content: 'PRIVATE_FILE'});
await tool('edit', {input: '[second.js#ABCD]\nPUT 1.=1:\n+PRIVATE_FILE'});
await tool('edit', {input: '[.maven/project.json#ABCD]\nPUT 1.=1:\n+PRIVATE_FILE'});
await tool('write', {path: `${home}/bookkeeping`, content: 'PRIVATE_FILE'});
await tool('write', {path: 'local://scratch', content: 'PRIVATE_FILE'});
await tool('write', {path: 'failed.js', content: 'PRIVATE_FILE'}, true);
await tool('write', {path: 'child.js', content: 'PRIVATE_FILE'}, false, child);
await tool('bash', {command: 'PRIVATE_COMMAND'});
await tool('bash', {command: "maven_turn -j '{\"data\":\"PRIVATE_FILE\"}'"});
await tool('bash', {command: 'maven_turn -j "$(PRIVATE_COMMAND)"'});
await tool('task', {task: 'PRIVATE_TASK'});
await emit('turn_end', {}, ctx);
assert.deepEqual(events().map(e => [e.phase, e.write, e.opaque]), [
  ['begin', undefined, undefined], ['observe', true, false], ['observe', true, false],
  ['observe', false, true], ['observe', false, true], ['observe', false, true],
]);
const followUp = await emit('session_stop', {}, ctx);
assert.deepEqual(followUp, {continue: true, additionalContext: 'Classify the actual work.'});
await emit('before_agent_start', {reason: 'prompt', prompt: followUp.additionalContext}, ctx);
await emit('agent_start', {}, ctx);
await emit('context', {messages: []}, ctx);
assert.equal(await emit('session_stop', {}, ctx), undefined);
assert.deepEqual(footers, ['fixture: verified']);
await emit('session_stop', {}, ctx);
assert.equal(footers.length, 1);
assert.equal(events().filter(e => e.phase === 'begin').length, 1);
await emit('before_agent_start', {prompt: 'Turn off MAVEN for this session.'}, ctx);
await emit('context', {messages: []}, ctx);
await tool('write', {path: 'off.js'});
await emit('session_stop', {}, ctx);
assert.equal(footers.length, 1);
// New extension instance: the stable native ID, not a transcript or cached flag,
// carries a persisted opt-out through resume/restore without a new owner prompt.
emit = harness();
await emit('session_start', {}, ctx);
await emit('agent_start', {}, ctx);
assert.match((await emit('context', {messages: []}, ctx)).messages.at(-1).content, /MAVEN is off/);
await tool('bash', {command: 'PRIVATE_COMMAND'});
await emit('session_stop', {}, ctx);
assert.equal(footers.length, 1);
await emit('before_agent_start', {prompt: 'Turn on MAVEN for this session.'}, ctx);
await emit('context', {messages: []}, ctx);
await emit('session_stop', {}, ctx);
assert.deepEqual(footers, ['fixture: verified', 'fixture: verified']);
const calls = events();
assert(!JSON.stringify(calls).includes('PRIVATE_'));
assert.deepEqual(calls.filter(e => e.phase === 'begin').map(e => [e.disable, e.enable]),
  [[false, false], [true, false], [false, false], [false, true]]);
for (const {phase, ...payload} of calls) {
  assert.equal(payload.conversation, 'native-conversation');
  assert.equal(payload.cwd, cwd);
  const keys = {begin: ['disable', 'enable', 'turn_id'], observe: ['opaque', 'turn_key', 'write'], end: ['turn_key']};
  assert.deepEqual(Object.keys(payload).sort(), ['conversation', 'cwd', ...keys[phase]].sort());
}

async function nextPrompt(context = ctx) {
  await emit('before_agent_start', {prompt}, context);
  return (await emit('context', {messages: []}, context)).messages.at(-1).content;
}
const cached = JSON.parse((await nextPrompt()).split('\n').at(-1));
assert.deepEqual(events().at(-1).known, {proj: '1'});
assert.deepEqual(cached.active.preferences, ['manual-commit preference 1']);
assert.equal(cached.turn_key, events().at(-1).turn_id);
assert.equal(cached.nexus, 'same_as_active');
await emit('message_end', {message: {role: 'assistant', content: [{type: 'text', text: 'Private answer.\nfixture: verified'}]}}, ctx);
await emit('session_stop', {}, ctx);
assert.equal(footers.length, 2, 'do not duplicate an already-correct reply footer');
writeFileSync(`${home}/version`, '2');
assert.match(await nextPrompt(), /manual-commit preference 2/);
await emit('session_stop', {}, ctx);
await emit('session_clear', {}, ctx);
assert.match(await nextPrompt(), /manual-commit preference 2/);
assert.equal(events().at(-1).known, undefined);
await emit('session_stop', {}, ctx);
writeFileSync(`${home}/fail`, '');
assert.match(await nextPrompt(), /local turn status could not be verified/);
await emit('session_stop', {}, ctx);
assert.match(footers.at(-1), /^MAVEN: failed/);
assert(!JSON.stringify(events()).includes('Private answer'));
