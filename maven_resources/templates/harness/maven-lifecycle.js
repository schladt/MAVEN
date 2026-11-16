// Native OMP API: v18.20.0 docs/extensions.md and extensibility/shared-events.ts.
// Requires OMP >=18.20.0 for the complete context API. Forced exits bypass this gate.
// https://github.com/can1357/oh-my-pi/blob/v18.20.0/docs/extensions.md
const MAVEN_HOME = __MAVEN_HOME_JSON__;
const HOOK_COMMAND = __MAVEN_HOOK_JSON__;

export default function mavenLifecycle(pi) {
    let conversation;
    let pending;
    let startupRequest;
    let shutdownRequest;
    let stopped = false;

    function identity(ctx) {
        const native = ctx.sessionManager.getSessionId();
        if (typeof native !== "string" || !native.trim()) throw new Error("Native OMP conversation ID unavailable");
        return native;
    }

    function bindingKey(binding) {
        if (!binding || typeof binding.session_id !== "string" || !binding.session_id || !binding.root) return undefined;
        return JSON.stringify([binding.root, binding.project_id, binding.session_id]);
    }
    function location(ctx) {
        // OMP materializes ctx.cwd per event; its session manager remains live across awaits.
        const cwd = ctx.sessionManager.getCwd();
        if (typeof cwd !== "string" || !cwd) throw new Error("Native OMP working directory unavailable");
        return cwd;
    }


    async function inspect(ctx, action, signal) {
        const result = await pi.exec(HOOK_COMMAND, [action, "--home", MAVEN_HOME, "--harness", "omp",
            "--cwd", location(ctx), "--harness-session", identity(ctx)], { cwd: location(ctx), timeout: 5000, signal });
        if (result.code !== 0 || result.killed) throw new Error(result.stderr || "Local lifecycle inspection failed");
        return JSON.parse(result.stdout);
    }

    function warn(ctx, error) {
        ctx.ui.notify(`MAVEN: ${String(error)}. Session kept open; no verified wrap-up.`, "warning");
    }

    function startupUnavailable(ctx, error) {
        ctx.ui.notify(`MAVEN startup unavailable: ${String(error)}. Readiness is not established.`, "warning");
    }

    function current(ctx, request) {
        return !stopped && identity(ctx) === request.native && location(ctx) === request.cwd;
    }

    async function startup(_event, ctx) {
        let request;
        let attempted = false;
        try {
            if (ctx.agent?.kind === "sub") return;
            if (ctx.agent?.kind !== "main" || typeof ctx.setTimeout !== "function") {
                throw new Error("Native lifecycle integration requires OMP 18.20 or newer");
            }
            const native = identity(ctx);
            const key = JSON.stringify([native, location(ctx)]);
            if (conversation === key) return;
            conversation = key;
            shutdownRequest = undefined;
            pending = undefined;
            stopped = false;
            request = { native, cwd: location(ctx), key };
            startupRequest = request;
            const fresh = () => startupRequest === request && current(ctx, request);
            const result = await inspect(ctx, "startup");
            if (!fresh()) {
                if (startupRequest === request) conversation = undefined;
                return;
            }
            if (typeof result.instruction !== "string" || !result.instruction) throw new Error("Startup context unavailable");
            // Generated instructions are agent-attributed, never fabricated owner consent.
            if (ctx.mode === "tui") {
                // sendUserMessage waits for the model turn, not just prompt admission.
                // Managed deferred work must not hold the host's startup event open.
                ctx.setTimeout(async () => {
                    try {
                        if (!fresh()) {
                            if (startupRequest === request) conversation = undefined;
                            return;
                        }
                        // Once called, a rejected promise may mean an admitted turn failed.
                        // Do not retry that ambiguous delivery on a later native event.
                        attempted = true;
                        await pi.sendUserMessage(result.instruction, { attribution: "agent" });
                    } catch (error) {
                        startupUnavailable(ctx, error);
                    }
                }, 0);
            } else {
                attempted = true;
                pi.sendMessage({ customType: "maven-startup", content: result.instruction, display: false },
                    { deliverAs: "nextTurn", triggerTurn: false });
            }
        } catch (error) {
            if (request && startupRequest === request && !attempted) conversation = undefined;
            startupUnavailable(ctx, error);
        }
    }

    pi.on("session_start", startup);
    pi.on("session_switch", startup);
    pi.on("session_branch", startup);

    pi.on("session_before_shutdown", async (event, ctx) => {
        if (ctx.agent.kind !== "main" || ctx.mode !== "tui") return;
        try {
            const snapshot = { native: identity(ctx), cwd: location(ctx) };
            shutdownRequest = snapshot;
            const fresh = () => shutdownRequest === snapshot && current(ctx, snapshot) && !event.signal.aborted;
            const state = await inspect(ctx, "status", event.signal);
            if (!fresh()) return { cancel: true };
            if (state.status === "closed" || state.status === "unbound") return;
            const exact = state.status === "open" && bindingKey(state.binding);
            const choices = exact
                ? ["Wrap up and exit", "Exit without wrapping up", "Stay"]
                : ["Exit without wrapping up", "Stay"];
            const choice = await ctx.ui.select(exact ? "MAVEN session is still open" :
                "MAVEN closure is unknown; automatic wrap-up is unsafe", choices,
                { initialIndex: choices.length - 1, signal: event.signal,
                    helpText: "Forced exit / another quit can bypass this dialog. Nothing is saved by the hook itself." });
            if (!fresh()) return { cancel: true };
            if (choice === "Exit without wrapping up") {
                pending = undefined;
                return;
            }
            if (choice !== "Wrap up and exit" || !exact) {
                pending = undefined;
                return { cancel: true };
            }
            if (pending) return { cancel: true };
            const request = { ...snapshot, key: exact, binding: state.binding, started: false };
            pending = request;
            // Return the veto before agent work is admitted. Managed timers run only after
            // this handler returns; never await agent work inside the host's quit gate.
            ctx.setTimeout(async () => {
                try {
                    if (!fresh() || pending !== request || ctx.hasPendingMessages()) {
                        if (pending === request) pending = undefined;
                        return;
                    }
                    request.started = true;
                    await pi.sendUserMessage(
                        "The owner selected Wrap up and exit in MAVEN's shutdown dialog. The quit was cancelled first. " +
                        "Wrap up only this exact MAVEN binding: " + JSON.stringify(request.binding) + ". " +
                        "Follow the MAVEN wrap-up procedure: call maven_pause(drain=true) to stop new admission " +
                        "while draining accepted work, then obtain a verified checkpoint, " +
                        "then call maven_finish for this same session. Never close peers, change projects, publish Git, " +
                        "or invent approvals. If any step fails or needs an owner choice, report it and keep the session open. " +
                        "The extension will request exit only after the exact verified finish receipt is present.",
                        { attribution: "agent" });
                } catch (error) {
                    if (pending === request) pending = undefined;
                    warn(ctx, error);
                }
            }, 0);
            return { cancel: true };
        } catch (error) {
            if (!event.signal.aborted) warn(ctx, error);
            return { cancel: true };
        }
    });

    pi.on("agent_end", async (event, ctx) => {
        const request = pending;
        if (!request?.started || stopped || event.willContinue || event.awaitingAsyncWork) return;
        try {
            if (!current(ctx, request) || ctx.hasPendingMessages()) {
                throw new Error("Conversation changed or additional work is queued");
            }
            const state = await inspect(ctx, "status");
            if (pending !== request || !current(ctx, request) || ctx.hasPendingMessages()) {
                if (pending === request) pending = undefined;
                return;
            }
            pending = undefined;
            if (state.status !== "closed" || bindingKey(state.binding) !== request.key) {
                throw new Error("The exact session has no verified finish receipt");
            }
            // OMP defers this supported request until the fully settled boundary.
            ctx.shutdown();
        } catch (error) {
            if (pending === request) pending = undefined;
            warn(ctx, error);
        }
    });

    pi.on("session_shutdown", async (_event, ctx) => {
        stopped = true;
        shutdownRequest = undefined;
        pending = undefined;
        if (ctx.agent.kind !== "main") return;
        try { await inspect(ctx, "shutdown"); }
        catch (error) { ctx.ui.notify(`MAVEN end event not recorded: ${String(error)}`, "warning"); }
    });
}
