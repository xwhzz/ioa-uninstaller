# iOA macOS 卸载方法与证据边界

这份文档描述一次拥有本机管理员权限的卸载实践。对应的发布脚本经过通用化改写；原始单机成功不等于发布脚本已经完成多机验证。

## 先确定归属

主应用的常见路径为：

```text
/Applications/QQPCMgr.localized/QQPCMgr.app
```

本次识别使用以下信息组合，而不是只看界面名称：

- 主应用 bundle ID：`com.tencent.QQPCMgr`。
- 安装收据：`com.tencent.iOA`。
- 签名团队 ID：`PGGDZWR6S3`。
- 已识别启动服务的执行文件位于该应用根目录或其明确的辅助应用目录内。

界面名称可以定制。其他腾讯应用不能仅凭厂商相同就认定为 iOA 残留。

## 服务与保活关系

本次真实运行时观察到了 `com.tencent.runmgr`、`com.tencent.QQPCGuardMonitor` 系统服务，以及 `com.tencent.QQPCMgr` 用户服务。系统服务配置含 `RunAtLoad` 和 30 秒 `StartInterval`。

`StartInterval` 是 launchd 的调度设置，不代表所有内部检查都按 30 秒执行，也不等于配置了 `KeepAlive`。用户服务 plist 中没有观察到 `KeepAlive`，其重新出现可由另一个后台组件重新加载来解释。

对原始 211.5 安装包的静态检查进一步发现：

- `ExitPromptDlg doModalExit` 读取 `ExitTrayPolicy`。
- `iOAUninstallAuthHelper uninstallAuthHandle` 读取 `UninstallPolicy`。
- `IOARummgrGuardManager` 检查服务禁用状态、plist 是否删除、服务是否加载，并包含重建和加载方法。
- `IOAGuardMonitorManager` 对另一个守护服务执行相似检查。
- `ZTSMPackageProtectionManager checkCoreProcess` 检查核心文件路径；满足配置、备份和版本条件时可调用重新安装流程。

这些名字是定位机制的线索。静态存在不证明每个分支在每台设备上都启用或执行。本仓库不包含反汇编输出或厂商源代码。

## 实际卸载顺序

先完整检查，再暂停本产品的重启监督组件。随后停止 iOA 自带 VPN、注销已识别启动服务、等待 launchd 完成异步退出。确认没有相关进程或服务重新出现后，才删除精确清单内的应用、恢复备份与配置。

暂停监督组件使用 `SIGSTOP`，使其不能在清理 VPN 和服务时立即重新拉起同伴。后续注销完成后结束这些进程；中途停止则尽力对仍是同一实例的暂停进程发送 `SIGCONT`。这不是全事务回滚。

不能省略恢复备份的识别。本次确认的目录包括：

```text
/Library/Application Support/QQPCMgrBackup
/Library/Application Support/QQPCMgrPKGBackup
/Library/Application Support/.ztsmconfig
```

还应将 `iOAGuarderLog` 纳入已知日志清单。后续审计发现该目录可能留下一份旧日志：日志存在不等于程序仍在运行，但也不能把它描述成“磁盘上已完全没有残留”。

## 三种不能混为一谈的失败

1. **应用自行恢复服务或进程。** 这是需要正确关闭保活链的原因。
2. **内置 VPN 清理工具返回非零。** 连接已关闭时也可能出现通信错误，必须核查实际进程状态；进程仍在则停止清理。
3. **清理脚本自身的检查错误。** 无关 plist 的非法 XML、launchd 尚在异步退出时过早判失败，都是脚本需要处理的情况，不能直接当作应用抵抗管理员权限的证据。

## 网络与系统限制

卸载应保存 DNS 和代理前后快照，但不要将其他 VPN 的断开、全局 DNS 重设、刷新防火墙或关闭 SIP 合并到通用流程里。网络副作用需要依据现场状态单独诊断。

若存在 iOA 系统扩展、登录机制或隔离配置，应先采用对应的受支持移除与恢复流程。此工具会拒绝自动处理这些环境，没有“强制忽略”选项。

## 如何报告新的验证结果

请记录 macOS 版本、CPU 架构、iOA 版本、安装根目录、是否有系统扩展/MDM、执行阶段及最终复查结果。去掉用户名、企业名称、服务器地址、租户 ID、设备标识、令牌和下载 URL 的签名参数。

一次 `--dry-run` 成功只说明检查过程完成；只有实际卸载后复查文件、运行进程、launchd 服务与安装收据，才能报告该环境的端到端结果。
