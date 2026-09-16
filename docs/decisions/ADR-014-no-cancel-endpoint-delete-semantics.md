# ADR-014: 上游没有取消端点时，`DELETE` 必须响亮失败

## Status
Accepted (2026-09-17)

## Background

适配范围在 2026-09-16 被冻结为「创建 + 查询」两个**核心**端点，列表与取消/删除是**可选**的
（`docs/upstreams/*` 一节，以及 `seedance-api-reference.md` §2）。
可选意味着**上游可能压根没有这个能力** —— 首个这样的上游已经出现：
`senseaudio`（`docs/upstreams/senseaudio-official-api.md` §2.1）只有
`POST /v1/video/create` 与 `GET /v1/video/status`，没有取消端点。

引擎原来的 `TaskManager.delete()` 是这么写的：

```python
if "cancel_request" in declared:
    ... 打上游取消 ...
# 没有该相位 ⇒ 直接落到下面
record = await self.store.update(local_id, status="cancelled", ...)
await self.gate.release(record["gate_key"], local_id)
return self._render(record)
```

⇒ 对"没有取消相位"的脚本，`DELETE` 一个 `queued` 任务会**在本地伪造 `cancelled`**，
而上游任务继续跑。三个后果都是实质性的：

1. **计费继续**：调用方以为任务停了，上游还在跑并继续计费 —— 而本层最贵的一类
   误判就是"钱在花而没人知道"；
2. **并发闸门少算**：槽位提前释放，而真实天花板是**在途数**（`ADR-013` 推论），
   于是闸门会放进比配置更多的在途任务；
3. **状态永久不一致**：本地 `cancelled`、上游 `running`，且**没有任何出口**能看出这件事
   （响应体只含原生字段，`ADR-011`）。

这条路径在只有 aivideomaker 一个上游时不会触发（它声明了 `cancel_*` 两个相位），
所以它是随"第二个上游"一起冒出来的沉默缺陷。

## Decision

**脚本未声明 `cancel_request` 相位时，对未终态任务的 `DELETE` 一律拒绝**：
400 `InvalidParameter`（`param=id`），消息说明"本渠道上游没有取消端点，
任务会继续在上游运行并计费；请轮询到终态后再 DELETE 删除本地记录"。

- 判断放在**发上游之前**，且**不动本地记录、不动并发槽位**（失败路径不释放槽位这一条
  本来就有：取消失败不能释放槽位）。
- **已终态任务的 `DELETE` 不受影响**：那一路在 `delete()` 更早处返回，语义是
  "删掉本地记录"（原生契约 §2.4），与上游有无取消端点无关。
- 出口码取 `InvalidParameter`(400) 而不是 `channel_config_error`：调用方的请求本身是合法的，
  缺的是**这个渠道能力边界**内的一个动作，与"上游不支持参考音频 ⇒ 400"同一类；
  `channel_config_error` 会把人送去查渠道头。

## Consequences

- **正面**：不会再有"本地显示已取消、上游仍在跑"的静默不一致；闸门不再少算在途。
- **正面**：能力缺口**响亮**暴露在接入阶段（第一次 `DELETE` 就撞上），而不是在账单上暴露。
- **负面 / 已接受**：调用方想中止一个跑错的任务时**做不到**，只能等它跑完。
  这不是本层的选择，而是上游没有这个能力 —— 把 400 说清楚比假动作诚实。
  若某天上游补上取消端点，只需在脚本里加 `cancel_request` / `cancel_response` 两个相位
  （`PHASES` 是脚本自己声明的），**引擎无需再改**。
- **负面 / 已接受**：出口用 400 `InvalidParameter` 表达"能力缺口"沿用了既有惯例
  （`docs/03_引擎架构.md` §10.2 的"本地：参考类素材能力缺失"同一形态）——
  官方码表里没有"上游缺这个操作"的专门 code，而**不发明新 code**是一条更硬的纪律。

## Related ADRs
ADR-002（路由与 provider）、ADR-011（响应体只含原生字段，所以不一致无法从响应体看出）、
ADR-013（真实天花板是在途数 ⇒ 槽位不能提前释放）
