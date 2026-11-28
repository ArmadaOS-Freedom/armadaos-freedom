function installTb322fcResolutions(resolutions) {
    if (location.origin !== "https://steamloopback.host" || window !== window.top) {
        return;
    }

    const installed = Symbol.for("armada.tb322fc.resolutions");
    const observed = new WeakSet();
    const gameSetter = "SteamClient.Apps.SetAppResolutionOverride(Vr.unAppID,ni.data)";
    const gameOptions = "ki=gt(),nn=(0,zt.EH)(Vr.strResolutionOverride,ki)";

    function appResolutionOptions(options, selected, internal) {
        if (!internal || !Array.isArray(options)
            || !options.some(option => option.data === "Native" && option.label?.endsWith("(3040x1904)"))) {
            return options;
        }
        const items = options.filter(option => option.data === "Default" || option.data === "Native");
        items.push(...resolutions.map(mode => ({data: mode, label: mode})));
        if (selected && !items.some(option => option.data === selected)) {
            const previous = options.flatMap(option => option.options ?? [option]).find(option => option.data === selected);
            if (previous) {
                items.push(previous);
            }
        }
        return items;
    }

    function wrapGameModule(chunk) {
        const factories = chunk?.[1];
        if (window[installed] || !factories || typeof factories !== "object") {
            return;
        }
        for (const [id, factory] of Object.entries(factories)) {
            if (typeof factory !== "function") {
                continue;
            }
            const source = Function.prototype.toString.call(factory);
            if (!source.includes(gameSetter) || !source.includes(gameOptions)) {
                continue;
            }
            const imports = [...source.matchAll(/\bzt=t\((\d+)\)/g)];
            if (imports.length !== 1) {
                continue;
            }
            const resolutionModule = Number(imports[0][1]);
            factories[id] = function(module, exports, webpackRequire) {
                // Only the game-properties module receives the model's list.
                const appRequire = new Proxy(webpackRequire, {
                    apply(require, receiver, args) {
                        const value = Reflect.apply(require, receiver, args);
                        if (args[0] !== resolutionModule || typeof value?.EH !== "function" || typeof value.Ck !== "function") {
                            return value;
                        }
                        return Object.create(value, {
                            EH: {value(selected, defaultLabel) {
                                const options = value.EH(selected, defaultLabel);
                                const internal = value.Ck()?.data?.is_internal === true;
                                return appResolutionOptions(options, selected, internal);
                            }},
                        });
                    },
                });
                return Reflect.apply(factory, this, [module, exports, appRequire]);
            };
            window[installed] = true;
            return;
        }
    }

    function observe(chunks) {
        if (!Array.isArray(chunks) || observed.has(chunks)) {
            return;
        }
        observed.add(chunks);
        const wrapPush = push => function(...entries) {
            entries.forEach(wrapGameModule);
            return Reflect.apply(push, this, entries);
        };
        let push = wrapPush(chunks.push);
        Object.defineProperty(chunks, "push", {
            configurable: true,
            get: () => push,
            set: next => { push = wrapPush(next); },
        });
        chunks.forEach(wrapGameModule);
    }

    const descriptor = Object.getOwnPropertyDescriptor(window, "webpackChunksteamui");
    if (descriptor && !descriptor.configurable) {
        return;
    }
    let chunks = window.webpackChunksteamui;
    observe(chunks);
    Object.defineProperty(window, "webpackChunksteamui", {
        configurable: true,
        get: () => chunks,
        set: next => {
            chunks = next;
            observe(next);
        },
    });
}
