# Steam bootstrap

`BASE.env` 固定 Steam stable manifest 版本与 runtime 版本及其 SHA256。
`refresh.py <manifest>` 校验本地 Valve manifest（含开头的 `linuxarm64` root、唯一的 ASCII 数字版本行、包名与 SHA256 格式）后，仅当版本更高时把官方原字节复制到 `manifests/<版本>` 并更新 `BASE.env` 的版本行；stdout 输出新版本号，同版或旧版无输出；已存在同字节同名清单但 BASE 仍旧时，重试仍会恢复并输出新版本。

## 自动跟踪

`.github/workflows/update-steam-bootstrap.yml` 每天 04:23 (UTC+8) 及手动触发，拉取固定的 Valve stable manifest，调用 `refresh.py`，仅有更新时直接运行 `python3 packages/steam-bootstrap/test.py`，在分支已存在时先经 GitHub compare endpoint 确认 `main...${branch}` 只改动两条路径并用 `git show FETCH_HEAD:<路径> | cmp -` 对比本地候选字节，非零 `ls-remote` 直接失败，然后在无需推送时补建 PR，以 `automation/steam-bootstrap-<版本>` 分支、关联 issue 与 PR 提交候选，不自动合并或批准。等待对应 PR 的镜像构建通过后人工合并。存在同版本 PR（无论状态）时直接跳过。

发布步骤使用仓库 secret `STEAM_BOOTSTRAP_UPDATE_TOKEN`：仅本仓库的 fine-grained PAT，授予 Contents、Issues、Pull requests 写权限。缺失时在发布 step 内发可读告警并跳过发布。

手动运行：到 Actions 页选择 Update Steam bootstrap → Run workflow（仅默认 main 分支生效）。合并前先看对应 PR 的构建结果。
