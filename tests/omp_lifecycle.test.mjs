// Offline consumer of the shipped module using the documented OMP v18.20 API surface.
// This does not substitute for the real OMP TUI verification described in the handoff.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = (await readFile(new URL('../maven_resources/templates/harness/maven-lifecycle.js', import.meta.url), 'utf8'))
    .replace('__MAVEN_HOME_JSON__', JSON.stringify('/private/home with spaces'))
    .replace('__MAVEN_HOOK_JSON__', JSON.stringify('/private/home with spaces/venv/bin/maven-session-hook'));
const { default: install } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const binding = { root: '/work/project', project_id: 'project', session_id: 'maven-session',
    harness: 'omp', harness_session: 'native-session', home: '/private/home with spaces' };

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
}

function consumer() {
    const handlers = new Map();
    const calls = [];
    const timers = [];
    let native = 'native-session';
    let state = { status: 'open', binding };
    let choice = 'Stay';
    let inspectionError;
    let sendError;
    let sendResult;
    let startupInspection;
    const context = {
        agent: { kind: 'main' }, mode: 'tui', cwd: '/work/project',
        sessionManager: { getSessionId: () => native },
        hasPendingMessages: () => false,
        setTimeout: (callback, ms) => { assert.equal(ms, 0); timers.push(callback); },
        shutdown: () => calls.push(['shutdown']),
        ui: {
            notify: (...args) => calls.push(['notify', ...args]),
            select: async (title, options, settings) => {
                calls.push(['select', title, options, settings]);
                return choice;
            },
        },
    };
    const pi = {
        on: (event, handler) => handlers.set(event, handler),
        exec: async (command, args, options) => {
            calls.push(['exec', command, args, options]);
            if (inspectionError) throw inspectionError;
            const output = args[0] === 'startup' ? { instruction: `Call maven_startup with ${native}` } : state;
            if (args[0] === 'startup') await startupInspection;
            return { code: 0, killed: false, stdout: JSON.stringify(output), stderr: '' };
        },
        sendUserMessage: async (...args) => {
            calls.push(['send', ...args]);
            if (sendError) throw sendError;
            return sendResult;
        },
        sendMessage: (...args) => calls.push(['context', ...args]),
    };
    install(pi);
    return { calls, context, timers,
        setState: (value) => { state = value; },
        choose: (value) => { choice = value; },
        native: (value) => { native = value; },
        failInspection: () => { inspectionError = new Error('offline'); },
        failSend: () => { sendError = new Error('prompt not admitted'); },
        setSendResult: (value) => { sendResult = value; },
        setStartupInspection: (value) => { startupInspection = value; },
        emit: async (event, values = {}) => handlers.get(event)?.({ type: event, signal: new AbortController().signal, ...values }, context),
        runTimers: async () => { for (const callback of timers.splice(0)) await callback(); },
    };
}

function exits(host) { return host.calls.filter(([name]) => name === 'shutdown').length; }
function sends(host) { return host.calls.filter(([name]) => name === 'send'); }

async function requestWrap(host) {
    host.choose('Wrap up and exit');
    const result = await host.emit('session_before_shutdown');
    assert.deepEqual(result, { cancel: true });
    assert.equal(sends(host).length, 0, 'quit must be cancelled before agent work is submitted');
    assert.equal(exits(host), 0);
    await host.runTimers();
    assert.equal(sends(host).length, 1);
    assert.match(sends(host)[0][1], /maven_finish/);
    assert.match(sends(host)[0][1], /maven-session/);
    assert.deepEqual(sends(host)[0][2], { attribution: 'agent' });
}

test('native session_start sends the instruction with the exact host identity, not PID or cwd', async () => {
    const host = consumer();
    await host.emit('session_start');
    const execution = host.calls.find(([name]) => name === 'exec');
    assert.equal(execution[1], '/private/home with spaces/venv/bin/maven-session-hook');
    assert.deepEqual(execution[2], ['startup', '--home', '/private/home with spaces', '--harness', 'omp',
        '--cwd', '/work/project', '--harness-session', 'native-session']);
    assert.equal(sends(host).length, 0, 'startup must return before admitting agent work');
    await host.runTimers();
    assert.match(sends(host)[0][1], /maven_startup/);
    assert.deepEqual(sends(host)[0][2], { attribution: 'agent' });
    await host.emit('session_switch');
    await host.runTimers();
    assert.equal(sends(host).length, 1);
    host.native('new-native');
    await host.emit('session_switch');
    await host.runTimers();
    assert.equal(sends(host).length, 2);
});

test('startup events finish while the deferred model turn remains unresolved', async () => {
    for (const event of ['session_start', 'session_switch', 'session_branch']) {
        const host = consumer();
        const turn = deferred();
        host.setSendResult(turn.promise);
        const completed = await Promise.race([
            host.emit(event).then(() => true),
            new Promise((resolve) => setImmediate(() => resolve(false))),
        ]);
        assert.equal(completed, true, `${event} must not await the model turn`);
        assert.equal(sends(host).length, 0);
        let settled = false;
        const delivery = host.runTimers().then(() => { settled = true; });
        assert.equal(sends(host).length, 1);
        await Promise.resolve();
        assert.equal(settled, false, 'the model turn is still pending after the handler returned');
        turn.resolve();
        await delivery;
        assert.equal(settled, true);
    }
});

test('deferred startup refuses shutdown, changed native identity, and changed workspace', async () => {
    for (const change of ['shutdown', 'native', 'cwd']) {
        const host = consumer();
        await host.emit('session_start');
        if (change === 'shutdown') await host.emit('session_shutdown');
        if (change === 'native') host.native('other-native');
        if (change === 'cwd') host.context.cwd = '/other/project';
        // Exercise even a callback retained past the host's automatic shutdown cleanup.
        await host.runTimers();
        assert.equal(sends(host).length, 0, change);
    }
});

test('switching away and back invalidates older deferred startup callbacks', async () => {
    const host = consumer();
    await host.emit('session_start');
    host.native('other-native');
    await host.emit('session_switch');
    host.native('native-session');
    await host.emit('session_branch');
    await host.runTimers();
    assert.equal(sends(host).length, 1, 'only the latest startup request may deliver');
    assert.match(sends(host)[0][1], /native-session/);
});

test('inspection completing after switch or shutdown cannot schedule stale startup', async () => {
    for (const mode of ['tui', 'print']) {
        for (const change of ['switch', 'shutdown']) {
            const host = consumer();
            host.context.mode = mode;
            const inspection = deferred();
            host.setStartupInspection(inspection.promise);
            const first = host.emit('session_start');
            host.setStartupInspection(undefined);
            if (change === 'switch') {
                host.native('other-native');
                await host.emit('session_switch');
            } else {
                await host.emit('session_shutdown');
            }
            inspection.resolve();
            await first;
            assert.equal(host.timers.length, change === 'switch' && mode === 'tui' ? 1 : 0);
            await host.runTimers();
            const deliveries = host.calls.filter(([name]) => name === 'send' || name === 'context');
            assert.equal(deliveries.length, change === 'switch' ? 1 : 0);
            if (change === 'switch') {
                const instruction = mode === 'tui' ? deliveries[0][1] : deliveries[0][1].content;
                assert.match(instruction, /other-native/);
            }
        }
    }
});

test('deferred startup reports both prompt admission and later model-turn rejection', async () => {
    for (const delayed of [false, true]) {
        const host = consumer();
        const turn = deferred();
        if (delayed) host.setSendResult(turn.promise);
        else host.failSend();
        await host.emit('session_start');
        const delivery = host.runTimers();
        if (delayed) turn.reject(new Error('model turn failed'));
        await delivery;
        const warnings = host.calls.filter(([name]) => name === 'notify');
        assert.equal(warnings.length, 1);
        assert.match(warnings[0][1], /MAVEN startup unavailable:.*Readiness is not established/);
        assert.match(warnings[0][1], delayed ? /model turn failed/ : /prompt not admitted/);
        assert.equal(warnings[0][2], 'warning');
        assert.equal(exits(host), 0);
    }
});

test('subagents inherit delegation; noninteractive sessions receive context without an unsolicited turn', async () => {
    const child = consumer();
    child.context.agent.kind = 'sub';
    await child.emit('session_start');
    assert.deepEqual(child.calls, []);
    assert.equal(child.timers.length, 0);
    const host = consumer();
    host.context.mode = 'print';
    await host.emit('session_start');
    assert.equal(host.timers.length, 0);
    await host.runTimers();
    assert.equal(sends(host).length, 0);
    const message = host.calls.find(([name]) => name === 'context');
    assert.match(message[1].content, /maven_startup/);
    assert.deepEqual(message[2], { deliverAs: 'nextTurn', triggerTurn: false });
});

test('Stay and dismissed dialogs cancel without agent work; exit-anyway does not close memory', async () => {
    for (const choice of ['Stay', undefined, 'Exit without wrapping up']) {
        const host = consumer();
        host.choose(choice);
        const result = await host.emit('session_before_shutdown');
        assert.deepEqual(result, choice === 'Exit without wrapping up' ? undefined : { cancel: true });
        await host.runTimers();
        assert.equal(sends(host).length, 0);
        assert.equal(exits(host), 0);
        assert.deepEqual(host.calls.find(([name]) => name === 'select')[2],
            ['Wrap up and exit', 'Exit without wrapping up', 'Stay']);
    }
});

test('wrap-up exits only after terminal settle and exact verified closure', async () => {
    const host = consumer();
    await requestWrap(host);
    host.setState({ status: 'closed', binding });
    await host.emit('agent_end', { willContinue: true });
    assert.equal(exits(host), 0);
    await host.emit('agent_end', { awaitingAsyncWork: true });
    assert.equal(exits(host), 0);
    await host.emit('agent_end');
    assert.equal(exits(host), 1);
    await host.emit('agent_end');
    assert.equal(exits(host), 1, 'no repeat exit request');
});

test('open, unknown, failed or mismatched finishes leave the conversation open', async () => {
    for (const state of [{ status: 'open', binding }, { status: 'unknown' },
        { status: 'closed', binding: { ...binding, session_id: 'another-session' } },
        { status: 'closed' }]) {
        const host = consumer();
        await requestWrap(host);
        host.setState(state);
        await host.emit('agent_end');
        assert.equal(exits(host), 0);
        assert.ok(host.calls.some(([name, message]) => name === 'notify' && message.includes('kept open')));
    }
    const host = consumer();
    await requestWrap(host);
    host.failInspection();
    await host.emit('agent_end');
    assert.equal(exits(host), 0);
});

test('unknown session warns and offers exit-anyway/stay, never an unsafe wrap-up', async () => {
    const host = consumer();
    host.setState({ status: 'unknown' });
    assert.deepEqual(await host.emit('session_before_shutdown'), { cancel: true });
    const selector = host.calls.find(([name]) => name === 'select');
    assert.match(selector[1], /unknown/);
    assert.deepEqual(selector[2], ['Exit without wrapping up', 'Stay']);
});

test('inspection failure vetoes normal quit and prompt admission failure never exits', async () => {
    const host = consumer();
    host.failInspection();
    assert.deepEqual(await host.emit('session_before_shutdown'), { cancel: true });
    const admission = consumer();
    admission.failSend();
    await requestWrap(admission);
    admission.setState({ status: 'closed', binding });
    await admission.emit('agent_end');
    assert.equal(exits(admission), 0);
});

test('native bypass/teardown and conversation change cancel any deferred wrap-up', async () => {
    const host = consumer();
    const abort = new AbortController();
    host.choose('Wrap up and exit');
    assert.deepEqual(await host.emit('session_before_shutdown', { signal: abort.signal }), { cancel: true });
    abort.abort();
    await host.runTimers();
    assert.equal(sends(host).length, 0);
    assert.equal(exits(host), 0);
    const switched = consumer();
    await requestWrap(switched);
    switched.native('other-native');
    switched.setState({ status: 'closed', binding });
    await switched.emit('agent_end');
    assert.equal(exits(switched), 0);
    const stopped = consumer();
    await requestWrap(stopped);
    await stopped.emit('session_shutdown');
    stopped.setState({ status: 'closed', binding });
    await stopped.emit('agent_end');
    assert.equal(exits(stopped), 0);
});

test('already closed and unbound sessions do not demand capture or wrap-up', async () => {
    for (const status of ['closed', 'unbound']) {
        const host = consumer();
        host.setState({ status, binding });
        assert.equal(await host.emit('session_before_shutdown'), undefined);
        assert.equal(host.calls.filter(([name]) => name === 'select').length, 0);
        assert.equal(sends(host).length, 0);
    }
});
