const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../system_files/usr/lib/armada/steam-game-resolutions.js"), "utf8");
const fixture = `
let internal = true;
let native = "3040x1904";
const defaultOption = {data: "Default", label: "Use global setting"};
const resolutionModule = {
    get EH() {
        return () => [
            {data: "Native", label: "Native (" + native + ")"},
            defaultOption,
            {label: "More", options: [{data: "3840x2160", label: "3840x2160"}]},
        ];
    },
    Ck: () => ({data: {is_internal: internal}}),
};
const games = {
    42: (module, exports, t) => {
        var zt=t(10);
        const gt = () => defaultOption.label;
        exports.menu = Vr => {
            const ki=gt(),nn=(0,zt.EH)(Vr.strResolutionOverride,ki);
            return nn;
        };
        exports.save = (Vr,ni) => SteamClient.Apps.SetAppResolutionOverride(Vr.unAppID,ni.data);
    },
};
webpackChunksteamui = [];
const originalPush = webpackChunksteamui.push.bind(webpackChunksteamui);
webpackChunksteamui.push = (...args) => originalPush(...args);
webpackChunksteamui.push([[], games]);
const exports = {};
games[42](null, exports, () => resolutionModule);
const forGame = selected => exports.menu({strResolutionOverride: selected});
`;

const context = vm.createContext({assert, location: {origin: "https://steamloopback.host"}});
vm.runInContext("globalThis.window = globalThis; globalThis.top = globalThis;", context);
vm.runInContext(`(${source})(["1600x900", "1520x952"]);` + fixture + `
assert.equal(window[Symbol.for("armada.tb322fc.resolutions")], true);
assert.equal(forGame("Default").map(option => option.data).join(), "Native,Default,1600x900,1520x952");
assert.equal(resolutionModule.EH().map(option => option.data).join(), "Native,Default,");
assert.equal(forGame("3840x2160").at(-1).data, "3840x2160");
assert.equal(forGame("1520x952").filter(option => option.data === "1520x952").length, 1);
internal = false;
assert.deepEqual(forGame("Default"), resolutionModule.EH());
internal = true;
native = "1920x1080";
assert.deepEqual(forGame("Default"), resolutionModule.EH());
`, context);

const incompatible = vm.createContext({assert, location: {origin: "https://steamloopback.host"}});
vm.runInContext("globalThis.window = globalThis; globalThis.top = globalThis;", incompatible);
vm.runInContext(`(${source})(["1520x952"]);` + fixture.replace("SetAppResolutionOverride", "UpdatedAppSetter") + `
assert.equal(window[Symbol.for("armada.tb322fc.resolutions")], undefined);
assert.deepEqual(forGame("Default"), resolutionModule.EH());
`, incompatible);
