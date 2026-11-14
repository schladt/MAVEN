// Native OMP API: v18.20.0 docs/extensions.md and extensibility/shared-events.ts.
// Requires OMP >=18.20.0 for the complete context API. Forced exits bypass this gate.
// https://github.com/can1357/oh-my-pi/blob/v18.20.0/docs/extensions.md
const MAVEN_HOME = __MAVEN_HOME_JSON__;
const HOOK_COMMAND = __MAVEN_HOOK_JSON__;

export default function mavenLifecycle(pi) {
    let conversation;
    let pending;
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

    async function inspect(ctx, action, signal) {
        const result = await pi.exec(HOOK_COMMAND, [action, "--home", MAVEN_HOME, "--harness", "omp",
            "--cwd", ctx.cwd, "--harness-session", identity(ctx)], { cwd: ctx.cwd, timeout: 5000, signal });
        if (result.code !== 0 || result.killed) throw new Error(result.stderr || "Local lifecycle inspection failed");
        return JSON.parse(result.stdout);
    }

    function warn(ctx, error) {
        ctx.ui.notify(`MAVEN: ${String(error)}. Session kept open; no verified wrap-up.`, "warning");
    }

    async function startup(_event, ctx) {
        try {
            if (ctx.agent?.kind === "sub") return;
            if (ctx.agent?.kind !== "main" || typeof ctx.setTimeout !== "function") {
                throw new Error("Native lifecycle integration requires OMP 18.20 or newer");
            }
            const native = identity(ctx);
            if (conversation === native) return;
            conversation = native;
            pending = undefined;
            stopped = false;
            const result = await inspect(ctx, "startup");
            if (typeof result.instruction !== "string" || !result.instruction) throw new Error("Startup context unavailable");
            // Generated instructions are agent-attributed, never fabricated owner consent.
            if (ctx.mode === "tui") {
                await pi.sendUserMessage(result.instruction, { attribution: "agent" });
            } else {
                pi.sendMessage({ customType: "maven-startup", content: result.instruction, display: false },
                    { deliverAs: "nextTurn", triggerTurn: false });
            }
        } catch (error) {
            ctx.ui.notify(`MAVEN startup unavailable: ${String(error)}. Readiness is not established.`, "warning");
        }
    }

    pi.on("session_start", startup);
    pi.on("session_switch", startup);
    pi.on("session_branch", startup);

    pi.on("session_before_shutdown", async (event, ctx) => {
        if (ctx.agent.kind !== "main" || ctx.mode !== "tui") return;
        try {
            const state = await inspect(ctx, "status", event.signal);
            if (event.signal.aborted) return;
            if (state.status === "closed" || state.status === "unbound") return;
            const exact = state.status === "open" && bindingKey(state.binding);
            const choices = exact
                ? ["Wrap up and exit", "Exit without wrapping up", "Stay"]
                : ["Exit without wrapping up", "Stay"];
            const choice = await ctx.ui.select(exact ? "MAVEN session is still open" :
                "MAVEN closure is unknown; automatic wrap-up is unsafe", choices,
                { initialIndex: choices.length - 1, signal: event.signal,
                    helpText: "Forced exit / another quit can bypass this dialog. Nothing is saved by the hook itself." });
            if (event.signal.aborted) return;
            if (choice === "Exit without wrapping up") {
                pending = undefined;
                return;
            }
            if (choice !== "Wrap up and exit" || !exact) {
                pending = undefined;
                return { cancel: true };
            }
            if (pending) return { cancel: true };
            const request = { native: identity(ctx), cwd: ctx.cwd, key: exact, binding: state.binding, started: false };
            pending = request;
            // Return the veto before agent work is admitted. Managed timers run only after
            // this handler returns; never await agent work inside the host's quit gate.
            ctx.setTimeout(async () => {
                try {
                    if (stopped || event.signal.aborted || pending !== request || identity(ctx) !== request.native) {
                        if (pending === request) pending = undefined;
                        return;
                    }
                    request.started = true;
                    await pi.sendUserMessage(
                        "The owner selected Wrap up and exit in MAVEN's shutdown dialog. The quit was cancelled first. " +
                        "Wrap up only this exact MAVEN binding: " + JSON.stringify(request.binding) + ". " +
                        "Follow the MAVEN wrap-up procedure: pause new automatic maintenance in this session, drain accepted work, " +
                        "and obtain a verified checkpoint, " +
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
            if (identity(ctx) !== request.native || ctx.cwd !== request.cwd || ctx.hasPendingMessages()) {
                throw new Error("Conversation changed or additional work is queued");
            }
            const state = await inspect(ctx, "status");
            if (pending !== request || stopped || identity(ctx) !== request.native) return;
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
        pending = undefined;
        if (ctx.agent.kind !== "main") return;
        try { await inspect(ctx, "shutdown"); }
        catch (error) { ctx.ui.notify(`MAVEN end event not recorded: ${String(error)}`, "warning"); }
    });
}
