# 后续工作

- [ ] 退出托管后转入 Desktop：按 harness 检测原生打开、导入或分叉能力；先释放 gateway 的 writer，再执行交接。保留原历史和身份映射，交接失败不回滚退出。没有可靠接口时提供原生 CLI 恢复入口；不直接拼改 Desktop 数据库。
- [ ] 一个机器级 gateway 承载多个 workspace；目前每个 bridge 对应一个 workspace，权限与 manager 管理范围按群聊中的该 workspace 隔离。
