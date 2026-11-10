# EDL 部署包构建

**简体中文** · [English](edl-package.md)

EDL 部署包是 9008 部署路径：它包含 Windows 安装器通过高通 9008（EDL）模式写入的原始镜像、内核、设备树和元数据。它与可刷写的 SD 卡镜像分开构建，也不取代现有 SD 镜像流程。

## 准备条件

- 具备 Docker 的 Linux 主机，容器可使用 loop 设备和 `SYS_ADMIN` 进行文件系统挂载，且主机支持 btrfs。
- 主机上的 Python 3。封装脚本在调用 Docker 之前会先运行主机辅助程序。
- `just` 与 `git`，并已递归检出子模块。
- 足够的磁盘空间，用于原始镜像、其 ZIP 分卷、拉取的容器镜像以及构建过程中在本地创建的容器镜像。

```console
$ git submodule update --init --recursive
```

## 构建部署包

```console
$ just build-edl-package \
    docker://ghcr.io/armadaos-freedom/armadaos-freedom:testing \
    sha256:DIGEST \
    FULL_SOURCE_COMMIT \
    EXACT_OCI_VERSION
```

| 参数 | 取值 |
|---|---|
| `IMAGE` | 包含真实可接受渠道（`stable`、`beta` 或 `testing`）的镜像引用，例如 `docker://ghcr.io/armadaos-freedom/armadaos-freedom:testing`。`docker://` 前缀可以保留但并非必需，封装脚本也接受不带前缀的裸仓库引用。构建据渠道推导签名镜像来源；`staging` 无法推导出可接受的签名来源。 |
| `DIGEST` | 该引用的准确 `sha256:` OCI 清单摘要，与标签分开提供。 |
| `REVISION` | 镜像构建时对应的完整 40 位小写提交 SHA。 |
| `VERSION` | 该镜像 `org.opencontainers.image.version` 标签的准确取值。 |

四个参数必须描述同一次构建：检出的 `HEAD` 必须等于 `REVISION`，摘要必须解析到所请求的镜像，镜像的 `org.opencontainers.image.revision` 和 `org.opencontainers.image.version` 标签必须分别匹配 `REVISION` 与 `VERSION`。不一致时构建会停止。渠道名或标签不能当作版本使用，也不能用旧提交搭配新镜像。

该配方只接收这四个位置参数，不会传递机型或配置选项，因此会打包 `build_files/edl-devices.json` 中登记的全部机型配置。部署包输出到 `output/edl/<version>/`，临时工作目录使用检出目录下新建的 `_build-edl.*`，该名称已被 Git 忽略。

如需只打包部分机型，或改用其他配置文件，请用同样的四个值直接调用封装脚本：

```console
$ bash /PATH/TO/repo/build_files/build-edl-package.sh \
    --oci docker://ghcr.io/armadaos-freedom/armadaos-freedom:testing \
    --manifest-digest sha256:DIGEST \
    --source-commit FULL_SOURCE_COMMIT \
    --version EXACT_OCI_VERSION \
    --source /PATH/TO/repo \
    --devices-config /PATH/TO/repo/build_files/edl-devices.json \
    --work /PATH/TO/edl-work \
    --output /PATH/TO/edl-output \
    --model TB322FC
```

`--model` 可重复指定，只打包所列机型；不指定时打包配置中的全部机型。`--devices-config` 默认使用源码树中的 `build_files/edl-devices.json`。

`--work` 与 `--output` 必须是绝对路径，且必须是新建或空目录。两者都不能是源码树根目录，也不能包含源码树；两者之间也不能相同或互相嵌套。允许使用源码树内部的新建目录，`just` 配方就是这样使用其中的 `_build-edl.*` 与 `output/edl`。完整 ZIP 保留在工作目录中；输出目录只写入分卷、分卷校验信息和部署包 README。

OCI 镜像包含构建系统镜像时生成的各机型 runtime。EDL 打包会复用内嵌载荷，并验证其 manifest、内核、initramfs、DTB 和配置。每个选中的机型都需要一份 runtime。若给出 `--runtime MODEL=绝对文件路径`，则必须为每个选中机型各提供一次，且每份所提供的 runtime 必须与内嵌 runtime 的哈希值和长度完全一致，不能覆盖内嵌 runtime。缺少所需 manifest 或组件的镜像将无法打包，必须使用匹配的源码和镜像输入重新构建。

## 打包本地构建的镜像

用 `just build` 构建的镜像带有版本标签，以及修订标签（构建时检出的提交）。用下面的命令打包：

```console
$ just build-edl-package-local ORIGIN [IMAGE] [TAG]
```

`ORIGIN` 是装好的系统用来更新的签名来源。本地镜像没有渠道，无法自动推导；封装脚本只接受 `ostree-image-signed:docker://ghcr.io/armadaos-freedom/armadaos-freedom:` 加 `stable`、`beta` 或 `testing`（可带 `@sha256:...`）。`IMAGE` 与 `TAG` 默认就是 `just build` 写出的镜像。配方用 `skopeo` 读取版本和修订标签，把镜像从本地容器存储复制成临时 OCI 布局（生成器按 `oci:<dir>:latest` 读取；这份副本有几 GB，结束后会删除），再用该布局、其清单摘要、两个标签和 `ORIGIN` 调用 `build-edl-package`。与该配方一样，检出的 `HEAD` 必须是构建镜像时的提交，并会打包全部已登记的机型。没有这两个标签的镜像（例如直接用 `podman build` 构建的）在复制前就会被拒绝。

本地主机与 CI 运行器有几处不同：

- 需要安装 `skopeo`。封装脚本用 `--cap-add SYS_ADMIN` 和 loop 设备运行 `docker`；在 podman 主机上，提供一个调用 **rootful** podman 的 `docker` 命令（例如 rootful podman machine 里的 `podman-docker`）即可。
- 生成器需要两个空闲的 `/dev/loopN`，否则报 "two unused /dev/loopN devices are required ...; found N" 并停止。精简的虚拟机里可能只有 `/dev/loop-control`，可以通过它添加 loop 设备，例如 `sudo python3 -c 'import fcntl,os; fd=os.open("/dev/loop-control",os.O_RDWR); [fcntl.ioctl(fd,0x4C80,i) for i in range(4)]'`（用 `LOOP_CTL_ADD` 添加 loop0-3）。
- 在启用 SELinux 的主机上（例如 Fedora CoreOS 的 podman machine），被绑定挂载的路径（源码、工作目录，以及 `--devices-config`、`--runtime` 指定的文件）要么打上 `container_file_t` 标签，要么让容器以 `label = false` 运行。把镜像导入 ostree 时还会写入镜像自身的 `security.selinux` 属性：生成器容器需要 `CAP_MAC_ADMIN`（否则报 `EACCES`）；主机以不认识这些标签的策略处于 enforcing 时，写入仍会报 `Invalid argument`，所以构建期间把主机设为 permissive（`setenforce 0`）。用一个 `containers.conf` 覆盖设置 `label = false` 并在 `default_capabilities` 中加入 `MAC_ADMIN`，可以满足容器这一侧；`CONTAINERS_CONF_OVERRIDE` 只在执行这条命令时设置，因为它对所有容器生效。

## 机型配置

`build_files/edl-devices.json` 使用 `schemaVersion` 1，包含一个全局 `runtimeSource` 和以机型 ID 为键的 `profiles` 对象：

| 字段 | 含义 |
|---|---|
| `schemaVersion` | 必须为 `1`。 |
| `runtimeSource` | runtime 快照相对源码树的路径，所有机型共用。 |
| `profiles` | 机型 ID 到配置的映射。由已登记的机型 ID 选择配置；流水线不维护固定的机型清单。 |
| `displayName` | 写入部署包 README 的机型名称。 |
| `compatible` | 写入单机型清单的 `soc` 与 `device` 兼容字符串。 |
| `dtb` | 设备树路径，相对于已部署镜像的设备树目录。 |
| `required` | 必须匹配的设备树兼容字符串。 |
| `forbidden` | 排除的设备树兼容字符串。 |
| `extraArgs` | 追加到该机型 normal 启动参数的额外内核参数。 |
| `runtimeConfig` | runtime 构建配置，相对于 runtime 快照。 |
| `runtimeCmakeArgs` | 构建该机型 runtime 时使用的 CMake 参数。 |
| `bootPartitionBytes` | 写入机型清单的声明启动分区大小。 |
| `diagnosticMode` | `rd-break` 或 `verbose`。 |

适配新硬件时，部署镜像中仍需有可用的 Linux 内核支持与匹配的设备树，runtime 快照中需有对应的 runtime 配置，Windows 安装器中需有对应的机型注册。仅添加一条配置项并不能让任意 Android 硬件获得支持。

## 内置插件来源

插件版本在构建操作系统镜像时确定，而不是在打包 EDL 部署包时确定。每次受支持的操作系统镜像构建——`just build` 配方、`build_push` 镜像任务和 PR 镜像构建——都会先依据 `decky/plugin-sources.json` 中的目录解析全部内置插件：

- release 条目解析为最新公开发布版本及其唯一匹配的资产，并按公布的 SHA-256 摘要校验；不使用固定的旧二进制。
- git 条目把配置分支的当前末端解析为一个不可变的 40 位提交 SHA，并构建该确切检出。
- in-tree 条目使用源码树中的包。

解析结果一次性写入 `decky/bundled-plugins/plugins.lock.json`、`plugins.resolved.json` 和 `assets/`，对应同一次选择。这些是生成产物，不是提交进仓库的旧二进制。无法解析的来源或摘要校验失败的资产会直接使构建失败，不会回退到旧缓存或旧锁文件。

该选择在当次镜像构建及其产出的 OCI 镜像内固定。EDL 打包只消费这份已冻结的镜像：不会重新解析插件，也不会在之后悄悄刷新内置内容。

## 持续构建

该构建也会自动运行。在 `main` 分支上，[`Build images`](../.github/workflows/build.yml) 中的 `build_edl_package` 任务在容器镜像任务之后执行，用刚推送的镜像构建 EDL 部署包，渠道为 `testing`，并使用该镜像的摘要、实际源码修订和 `org.opencontainers.image.version` 标签。部署包作为 `edl-package-<运行编号>` 工作流产物上传，在运维人员发布之前只停留在此处；不会自动创建 Release，也不会自动改动任何更新渠道。SD 镜像流程继续保留，其自动发布需按下文配置 R2 并启用。

也可以手动启动 [`Build EDL package`](../.github/workflows/build-edl-package.yml)：它接收与配方相同的四个值，按指定修订递归检出子模块，并把分卷、`parts.json`、`SHA256SUMS` 和部署包 README 作为工作流产物上传。

## 发布更新渠道

镜像自带 `/etc/armada/updates/channels.json`，把每个渠道指向 `update-channels` 分支上的签名文档：`https://raw.githubusercontent.com/ArmadaOS-Freedom/armadaos-freedom/update-channels/<渠道>.json` 及其 `.json.sig`。三个渠道都会列出，但只有运维人员真正发布后渠道才可用：设备拉取该文档并校验签名，不会回退到其它渠道或来源。

流程是：构建 → 发布 testing → 晋升。

1. 构建部署包：`main` 上的 `Build images`，或手动运行 `Build EDL package`。
2. 用已完成的运行编号和 `testing` 运行 **Publish update channel**。它会确认该运行属于本仓库、已成功，并产出了 `edl-package-<运行编号>`。随后校验描述签名、分卷、分卷清单、`offline-upgrade` 能力，以及 OCI 摘要确实解析到由该源码修订构建的签名镜像。然后创建草稿 Release `<releaseId>`，上传分卷、分卷清单、描述文件和描述签名，等全部资产到位后才公开 Release，最后才签名并提交 `testing.json` 与 `testing.json.sig`。
3. 运行 **Promote release**，`source: testing`，`target: beta` 或 `stable`。晋升读取同一份签名渠道，要求源标签解析到完全相同的摘要和镜像修订，再把目标渠道指向同一批 Release 资产。全程不重新构建；没有匹配的签名渠道发布时，源标签会在打任何标签之前就失败。

不会就地替换：已公开的 Release 必须与校验过的部署包完全一致；草稿 Release 即使是空的也会就地补全——只上传缺失的资产，已存在的资产按签名计划做哈希校验而不是覆盖；内容不一致的 Release 会直接失败。渠道文档与签名以一次提交写入 `update-channels` 分支的对应文件，更新串行执行，绝不强制推送。

## 部署包结构

部署包使用 `schemaVersion` 2，部署类型为 `edl-raw-v1`：

- 启动与根文件系统的原始镜像、内核和 initramfs 只存一份，由所有选中机型共用。
- 每个机型保留各自的 runtime、normal 与 diagnostic 设备树启动镜像，以及各自的清单。
- 共用载荷不代表不同机型的启动镜像可以互刷，仍需使用与机型匹配的镜像。

完成的 ZIP 按固定 1900 MiB 切分为原始字节分卷，不做重新压缩，命名为 `<名称>.zip.001`、`<名称>.zip.002`……最后一卷较短。`<名称>.parts.json` 与 `SHA256SUMS` 作为分发完整性记录，保存分卷信息及校验和。每个分卷小于 2 GiB，以便作为发布资产；这是部署包自身的约定，不是 Actions 产物的限制。

## 诊断启动模式

每个机型配置声明一种诊断模式：

- `rd-break` 在 normal 启动参数后追加 `rd.break=pre-mount`。
- `verbose` 去掉 `quiet`、所有 `loglevel=` 开关以及所有 `rd.break` 变体，保留其余 normal 参数（含机型额外参数），并确保存在 `loglevel=8 ignore_loglevel printk.time=1 initcall_debug rd.debug`，缺失的参数按此顺序追加。其输出不含 `rd.break`。

四代机型（TB322FC）使用 `rd-break`；五代机型（TB323FU）使用 `verbose`，其机型参数通过 `extraArgs` 提供。小新 Pad Pro GT（TB710FU）使用 `rd-break`，并通过 `extraArgs` 传入 `clk_ignore_unused pd_ignore_unused regulator_ignore_unused`：Linux 因此不会关闭引导程序留下的时钟、电源域和稳压器，包括它仍在扫描输出的显示所用的那些。

`runtimeCmakeArgs` 是按机型配置的源码选项。五代配置传入 `-DKSHIM_TB323FU_ANDROID_TOUCH_4M8_TRIAL=ON`，在交接时把原 Android 触摸控制器限制为 4.8 MHz；四代和 TB710FU 配置传入 `OFF`，CMake 默认值也是 `OFF`。

## 使用 Windows 安装器刷写

把同一个部署包的所有分卷放在同一文件夹中，不要改名，并在匹配的安装器中选择 `.zip.001`；安装器会从该文件夹读取连续编号的分卷，不读取 `parts.json`。

部署包把源码记录为来源信息：一个准确、非全零的 40 位小写提交 SHA，读取时不裁剪、不做规范化。[Windows 安装器](https://github.com/ArmadaOS-Freedom/armadaos-freedom-installer)负责校验该来源信息，并选择与目标平板匹配的型号配置。

安装器使用本机已校验的恢复资料生成平板专属的 2 GiB `ARMADA_BACKUP`，它不属于共用 Linux 镜像。安装器 0.7.4 新建恢复镜像采用 v3，在最后 64 KiB 留出启动设置区域；各型号能否读取或保存设置仍由各自的启动器实现决定。详见[恢复区约定](https://github.com/ArmadaOS-Freedom/armadaos-freedom-installer/blob/main/docs/package-contract.md)。

原有的 SD 卡镜像及其手动安装流程仍然可用。

## 发布配置

正式镜像地址为 `ghcr.io/armadaos-freedom/armadaos-freedom`。`Build images` 从 `main` 发布签名后的 `testing` 镜像；`Promote release` 核验同一镜像摘要后，再设置 `beta` 或 `stable` 标签。将加密的 Cosign 私钥配置为 `SIGNING_SECRET`，密码配置为 `SIGNING_PASSWORD`；对应公钥位于 `system_files/etc/pki/containers/armadaos-freedom.pub`。私钥不得放入源码。

GHCR 包需设为公开，并允许本仓库的 Actions 访问；工作流在发布渠道标签前检查匿名读取。构建好的部署包先保存在 `edl-package-<run-id>` 产物中，由 **Publish update channel** 转成 Release 与签名的 `testing` 渠道；beta 与 stable 只能由 **Promote release** 跟进。镜像自带的 `system_files/etc/armada/updates/channels.json` 列出全部三个渠道，只有对应文档出现在 `update-channels` 分支后该渠道才可用。原有磁盘镜像的 R2 发布按需启用：先配置 R2 地址、桶和凭据，再设置仓库变量 `ARMADA_PUBLISH_DISK=true`。

Windows 安装器 0.7.32 起识别组织地址，并继续兼容迁移前 `mydanyi` 镜像地址的安装包。使用新组织地址的包之前，请先升级安装器；旧版会拒绝尚不认识的来源。新包和更新目标使用组织地址，不改写历史包的来源记录。

## 包签名与升级能力

构建封装脚本在归档及分卷生成后，输出 .zip.update.json 描述文件，绑定发布版本、源码提交、OCI 摘要、OSTree 提交、支持机型、有效载荷路径、压缩及展开大小和完整摘要；共用系统文件只记录一份。

本地封装脚本输出的描述尚未签名。EDL Actions 工作流对其原始 UTF-8 字节使用发行密钥签名，以固定公钥核验后，把 .zip.update.json.sig 与分卷一起上传。SHA256SUMS 和 parts.json 不能替代发行签名，也不能信任安装包自行提供的公钥。

生成器先确认部署镜像带有正常 Linux 更新器，随后声明 install 与 offline-upgrade；镜像缺少该更新器时构建直接失败，不会被改标为可升级。离线升级保留用户数据，并用已校验的载荷写入两个启动槽；在线 OCI 更新只轮换非当前槽。签名本身不使包具备升级能力：描述必须声明 offline-upgrade，且已安装更新器版本不低于 `minimumUpdaterVersion`。

离线升级目前仅支持联想 Legion Y700 四代（TB322FC）与五代（TB323FU）。联想小新 Pad Pro GT（TB710FU）仍可能出现在通用首次安装包中，但其更新槽后端尚未实现，因此出现在部署包的机型列表中并不代表该机型支持离线升级。

恢复描述属于内存救援路径，不是本包的前提：本包不附带救援产物，该对象可以缺省或不完整；凡出现的条目仍按同一模式与文件记录逐项校验。渠道生成工具仍会拒绝只有首次安装能力的描述。

.github/scripts/make-update-channel.py 从已签名且具备升级能力的描述生成待签名渠道文件。提升渠道时保留同一个发布版本、源码提交、OCI 摘要、OSTree 提交和包身份，不重新构建系统；verify 子命令分别验证渠道和包的签名，再检查两者绑定关系。生成或核验文件本身不会发布渠道或修改镜像标签；实际发布时用 `SIGNING_SECRET` 对渠道文档签名，并把文档与签名一起提交到 `update-channels` 分支，如上文所述。

## 测试

```console
$ just test-edl-package
```

该命令运行 EDL 文件系统测试，需要 Docker 以特权方式访问 loop 设备与 btrfs。该配方与 `just test` 分开。

## 系统更新引导工具的构建来源

正式镜像通过 `build_files/prepare-boot-tool-source.py` 从安装器仓库的 `main` 取得 BootTool 所需源码，先固定实际提交，再生成文件摘要清单。引导重打包和签名逻辑由安装器 Core 复用；不会另维护一份系统侧实现。所需源码缺失时构建失败，不能静默生成缺少更新引导工具的镜像。

`Containerfile` 的 `update-boot-tool` 阶段把包含运行时的 ARM64 工具安装到 `/usr/libexec/armada/boot-tool/`，并在 `/usr/share/armada/updates/boot-tool-source.json` 保存来源记录。`just build` 和镜像 CI 均包含这一步，SDK 只留在构建阶段。

离线开发可通过 `ARMADA_BOOT_TOOL_LOCAL_SOURCE` 为 `just build` 指定本地安装器源码；来源记录会明确标为 `local`，不把它冒充已发布提交。正式 CI 使用远端源码。跨仓库更新需先合入安装器端 BootTool 源码，再合入系统侧依赖它的构建变更。
