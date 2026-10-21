---
name: auto-continue
description: 排查和配置 Auto-Continue —— Claude Code 在 Windows 上的看门狗。看懂它的活动日志（每行都带窗口 id）、判断某个会话为什么没被接上、配置「跑完接下一棒」、把日志脱敏后发给作者。触发词：auto-continue, 自动继续, 会话卡住没人管, 5 小时限制没恢复, 窗口没被接上, activity.log, 看门狗, watchdog, 安全拦截, after-finish, 导出日志反馈。
---

# Auto-Continue

Claude Code 的 Windows 看门狗：盯着「窗口停下来了」并替用户推下去。
单文件免安装，MIT 开源、免费、可商用。

### 3.3.0 Claude EXE（Claude 桌面版）

新增第四种监测类型，**默认关闭**，在 Claude EXE 页签勾选「Monitor this type」开启。只处理每个 Claude 窗口里**当前显示**的那个 Code 会话：
页面标题是「<会话标题> - Claude Code」，Claude Code 也把同一标题写进该会话的记录文件（`%USERPROFILE%\.claude\projects`），
标题能唯一对上一个桌面会话才动手。重名、分屏开了多个会话、找不到记录时一律不动，日志写一次原因
（`… is not acted on (another session has the same title; …)` 之类）。
额度用完：等重置时间 + Buffer 后发 `continue`（桌面版自己也可能在那个时间续接，Buffer 让它先来）；
连接出错：每个 Retry 间隔发一次，最多 Maximum retries 次；After finish：每个完成的回合一次。
安全拦截、模型自己的额度、登录问题不打字，标为 Needs attention。
打字方式是写进输入框再按应用自己的 Send 按钮，不模拟按键；会话记录里出现这条新提示才算发送成功，
日志写 `Claude EXE #… : … submission confirmed`。权限提示和提问卡片留给用户；不改模型和推理强度。

### 3.2.0 Claude CLI 精简

3.2.0 删除了 Claude CLI 的**模型恢复**（被安全拦截时切到备用模型、跑完 `/compact`
再切回，以及额度用完自动换模型、把窗口拉回目标模型）。被安全拦截或模型额度用完的会话
现在**原样等人处理**：不打任何字，after-finish 也不把它当成「跑完了」，安全拦截的选择框和
*Switch model?* 对话框永远不会被自动回车。以前开着模型恢复的人，首次启动时日志会写一次
`model recovery was removed in 3.2.0 and no longer runs`；旧设置原样留着不再读取。
同版还去掉了「有已知窗口没列出来就等 2 秒再列一遍」这一步（它会让退出 Claude 后留下的
终端每轮都白等 2 秒）、v2.0.6 的 after-finish 迁移和设置对话框里没用的 General 页。
限额、网络、选择框、after-finish、模型/effort 覆盖、Triggers 和本机接口行为与 3.1.2 相同。

### 3.1.1 独立 EXE 升级

- **Update now** 一次完成下载、SHA-256 校验和重启，并恢复升级前的三个监测开关及设置。
- 原 EXE 备份为同目录下的 `Auto-Continue.exe.bak`。下载或校验失败不会安装；替换失败会重新打开原版本，并在下次启动的日志中说明原因。
- Control 继续使用其内置副本，由 Control 包管理。不要手动覆盖内置 EXE 或删除版本标记。独立版升级不要求关闭 Control。

3.1.2 另修复旧独立版首次升级后的 Python DLL 启动错误。旧更新器可能仍保留一次原有的重启确认；升到 3.1.1 及以上后的 Update now 无需第二次确认。

**先读日志再下结论。** 它是屏幕抓取工具，凭记忆猜它「应该」怎么做，
十次有九次猜错——日志里写着它当时看见了什么、做了什么。

## 东西在哪

| | |
|---|---|
| 活动日志 | `%LOCALAPPDATA%\auto_continue\activity.log` |
| 轮转 | 到约 1 MB 转成 `.log.old`，只留一份；事发第二天排查记得连 `.old` 一起看 |
| 设置 | 注册表，随 GUI 保存；界面上 **Advanced…** 里全部可改 |
| 日志本体 | 只记它自己的判断和动作，**从不记录终端内容** |

```bash
tail -40 "$LOCALAPPDATA/auto_continue/activity.log"
```

## 怎么读一行日志

```
2026-09-06 07:33:31  [warn]  limit on '◑ 某会话' #093c → resets 10:40pm …
                     级别         事件            窗口标题   窗口 id
```

- **`#093c` 是窗口 id**（2.0.17 起）。标题会随对话改名、也可能重名，
  **认 id 不认标题**。同一个 id 就是同一个窗口；窗口关掉重开会换 id。
- 级别：`info` 记录 / `warn` 需要注意 / `fire` 真的发出了按键 / `err` 出错。
  **数「打了多少字」就数 `fire` 行**：一次故障里反复重发，每一次都记 `fire`
  （2.1.2 起；之前第二次往后记成 `info`，数出来会偏少）。托盘气泡只在第一次弹，
  但日志和接口事件一次不少。

### 两种「沉默」必须分清（2.0.18 起日志会直说）

```
[warn]  could not read the screen of #093c this pass — treating it as unknown, not as 'nothing there'
[info]  screen of #093c readable again
```

Windows 的 UI Automation 偶尔会拒绝一个窗口，这时它**什么都没看到**——
和「看了，屏幕上没问题」完全是两回事。见到上面这行，就别再把同一时刻的
「没检测到异常」当成「一切正常」。

## 它管四种情况

| 日志里长这样 | 它做了什么 |
|---|---|
| `network retries exhausted` / `network API error` / `response truncated mid-stream` | 每隔重试间隔发一次 `continue`，直到会话重新开始输出 |
| （屏幕上）`No response from the API after 6m · retrying once, waiting up to 10m` | **什么都不做**：这是 Claude Code 自己在重试，算「正在运行」。重试也失败、出现单独一行的 API 报错说没有响应时，才按网络错误处理 |

网络连接错误认的是**括号里的大写错误码**（`ECONN·RESET`、`ETIMEDOUT` 这类），
不是前面那句话（2.1.4 起）——那句话 Claude Code 两周里改了三次措辞，每改一次就有会话被晾着。
某个会话明明停在一条带错误码的 API 报错上却没被推，先看是不是错误码不在括号里。

网络类报错**必须单独成行**才算数（2.1.3 起）。夹在一句话中间的同样文字，是有人在引用它，
通常是 Claude 在解释你贴的报错，不会被当成卡住去发 `continue`。
| `limit on … → resets …; will fire at …` | 等到重置时间 + Buffer，再发 `continue` |
| `answered the permission prompt` | 回车放行安全拦截（默认开） |
| `answered the chooser` | 回车选中默认项（**默认关**，要自己打开） |

配套的两句话：

- `network error cleared on … ; recovered` —— 会话重新开始输出了，戳有效果。
- `limit message gone on … before fire; assuming handled manually` ——
  到点前发现横幅没了（用户自己继续了），于是**放弃**发送，不多打一个 `continue`。

## 它**故意**不管的，别劝用户去「修」

1. **限制到期时那个「要不要买额外用量」的选择框** —— 另一个选项是花钱，
   回车会选中高亮项。这是钱的决定，屏幕抓取不该替人做。它只标记 `⏎ Limit prompt` 等你。
2. **登录过期** —— `OAuth token expired … can't fix this; run /login in that session`。
   打字救不回来，所以只报一次，不骚扰。**让用户去那个会话里 `/login`**。
3. **正在输出的会话** —— 到点的 `continue` 会等它安静下来再发，日志写
   `is due but the session is mid-turn; holding the 'continue' until it goes quiet`。
   这不是卡住，是刻意的。
4. **安全拦截和切换模型的对话框** —— 安全拦截提示（`… safeguards flagged …`）不打任何字；
   它附带的选择框和 *Switch model?* 对话框回车就会换模型，那是用户的决定，永远不自动回车。
   模型额度用完（`You've reached your <模型> limit …`）同样原样留给用户——3.2.0 起不再自动换模型。
4b. **输入框里有没发出的字，或者有问题/权限提示开着**（2.1.4 起）——
   网络 `continue`、限额恢复、after-finish 都**先等着**，日志写一次
   `… but the input box holds unsent text` / `a prompt is open and waiting for an answer`
   或 `after-finish on … is held`。
   打字总以回车结尾：接在草稿后面会一起提交，按在权限提示上等于选了「Yes」。
   用户把草稿发出去或清掉、把提示答掉，下一轮就照常发。**Fire now** 不受影响。
5. **配套程序把自动回车关了** —— 日志 `api: settings auto_permission=off`
   （或 `auto_choose=off`）。这是本机另一个程序（例如手机上远程批准的那种）
   通过本地 API 关的，好让人来决定，Advanced 里的勾也跟着变了。之后的权限提示
   不再自动放行是**预期行为**。`api: keys …` / `api: send …` 同理：那是配套程序
   打的字，不是本工具自己发的。这两行只记**动作**不记内容 ——
   `lines=1`、`<24 chars>{Enter}`（2.1.2 起；键名保留，打的字只剩字数）。
   想知道对方到底打了什么，去问那个程序，日志里没有。
   配套程序收到 `busy` 就表示**什么都没做**（2.1.4 起），可以放心重试；
   2.1.3 及之前 `busy` 之后动作仍可能在本轮巡检结束后被执行，
   重试就会重复输入。若日志里同一窗口在几秒内出现两条内容相同的 `api: send`，多半是这个原因。
   窗口停在选项框上时（权限提示、提问、多题表单、计划确认），那一行的 `prompt` 会带完整结构
   （2.1.4 起）：问题、标题、页签、每个选项（编号、说明、光标、勾选、是否已选、是否是自己输入行）、
   分隔线下的行、脚注。光标不在第 1 项也照样报——这只是「报」，本工具自己的自动回车仍然只在光标停在第 1 项时才按。
   屏幕上读不出的字段是 null，不猜；会话内容（diff、计划全文）不给。
   同一个键连按三次以上，日志记成 `{Back}×80` 这样的计数。
   配套程序看到的窗口列表（`/windows`）每行带 `read_utc`，即那一行最后一次读屏的时间（2.1.4 起）；
   单窗口重读（`/scan`）会先把结果写进列表再回复，两者不会再一个说有提示、一个说没有。
   `api: send … not typed — the input box holds unsent text`（2.1.4 起）：
   那个窗口的输入框里有人打了一半的字，配套程序拿到 `draft`，什么都没打、也没清。
   以前会把新内容接在草稿后面一起提交。
   输入框里的**灰字建议**（Claude Code 回合结束后给的下一句提示，Tab 采纳）不算草稿（3.3.2 起）：
   按颜色和光标区分——比提示符暗、光标停在它前面的是建议；正常颜色的字才是草稿。
   3.3.1 及之前会把建议当草稿，于是限额续接、After finish 和配套程序的 send 一律被拦（`draft`）。
   `api: send … (refused: not_confirmed)` / `(refused: focus_lost)`、
   `send failed for … (not_confirmed)`（3.3.2 起）：每行字要先在输入框里看到、
   回车后再看到它离开输入框，才算发出去、才把原前台窗口还回去。`focus_lost` 是打到一半
   别的窗口抢了前台；`not_confirmed` 是打完了但屏幕上没看到全部到达（这时不按回车），
   或按了回车没看到被取走。这两种情况输入框里可能留着半行字，先看屏幕再决定。
   3.3.1 及之前长句末尾几个字和回车可能落到原前台窗口，接口却回 `ok`。
   配套程序拿到 `bad_keyspec` 说明键名写错了（退格是 `{Back}`，不是 `{Backspace}`），
   重试没用；这种请求什么都没做，所以日志里也没有它。
   `%APPDATA%\Auto-Continue\local_api.json` 里的 `exe` 是当前运行那一份的路径（2.1.4 起），
   配套程序要重新拉起本工具时应该启动这一份，而不是它自己内置的旧版。

## 排查：某个会话没被接上

按顺序问日志，别跳步：

1. **这个窗口在覆盖范围里吗** —— 日志里有没有出现过它的 id？
   没有就是没被看见：检查是不是被 **Exclude** 掉了，或者那个 Claude Code
   开在了 Windows Terminal 的**标签页**里——只有**当前活动标签**会被读到，
   每个会话拖成独立窗口才都能被看住。
2. **当时读得到屏幕吗** —— 附近有没有 `could not read the screen of #id`。
   偶发一两轮是 UIA 打嗝，会自己好（好了会写 `screen of #id readable again`）。
   另一种是整个窗口这一轮没枚举到：`N open window(s) missing from this pass (#id …; not readable as a Claude session right now)`
   —— 状态会保留，不会重来；2.1.2 起括号里写明是哪几个窗口，
   **同一个 id 反复出现**才值得查，零星几次是正常的。
   2.1.4 起窗口列表直接向 Windows 要，不再让 UIA 枚举整个桌面，
   这行警告应该很少见了；2.1.4 之后还频繁出现，就值得报给作者。
   **一直不好、再也没恢复**，多半是这个窗口的会话进程已经被杀掉了，
   而终端窗口壳子还留着（典型：某个脚本 `taskkill /PID <claude> /T /F` 之后没关窗口）。
   这种窗口 Win32 看还在、UIA 也枚举得到，但 ConPTY 已死，文本读取永远 E_FAIL ——
   **它不会自己恢复，关掉这个窗口即可**。在它关掉之前，这个"窗口"一直占着一格覆盖数，
   而里面根本没有会话。
3. **它是不是判断成「不该动」** —— 看有没有上面「故意不管」的那几条。
   还有一种：`… is scrolled up (Claude Code shows 'Jump to bottom')`——
   那个会话被往上翻了，屏幕上看不到它当前的状态，工具就先不动它；翻回底部会写
   `… is back at the bottom`，然后照常处理。翻上去超过 60 分钟（Advanced → Answering 可改，0=不管）
   会自动发一次 Ctrl+End 拉回底部：`scrolled … back to the bottom (Ctrl+End) after 60 min`
   ——只移动视图，不往会话里打字。
4. **发了但没送达吗** —— `retry send failed for …; will try again`
   说明按键没送出去（通常是发送瞬间焦点被别的窗口抢走），下一轮会自己重来。
5. **是不是根本没到时间** —— `will fire at …` 那行写着预定时刻，
   实际触发会晚一个轮询周期以内（默认 60 秒），这是正常的。

## 常用设置（Advanced…）

- **Poll interval**（默认 60 秒）读每个窗口的间隔；**Buffer**（默认 60 秒）
  重置时间之后多等一会儿再发，因为限制常常比写的时间晚一点点解除。
- **Retry interval**（默认 600 秒）网络卡住时两次 `continue` 的间隔。调太短只会刷屏。
- **Triggers** —— 四类横幅的识别正则可以自己改。Anthropic 改文案时这里先失效，
  也是第一个该看的地方。
  自己改正则时别让几个可省略的空白（`\s*`）挨在一起：屏幕每行都用空格补满，
  这种写法在空屏上要算好几秒，而且每个窗口每轮都算一遍（2.1.4 前内置的安全拦截正则就是这样）。
- **跑完接下一棒（after-finish）** —— **默认关闭、按窗口开启**；
  带次数上限，默认只跑一次，用完要手动再开。

## 报问题给作者

`Export log…`（2.0.18 起，主界面按钮；2.1.4 前叫 Export log for feedback…）导出一份**脱敏**日志：
窗口标题和用户自己输入的提示词全部抹掉，窗口只以 `#id` 出现，
斜杠命令和 `continue` 保留（它们不描述任何工作内容）。
**写盘前会把全文摊出来让人过目**，确认了才保存。

原始日志留在本机没问题，但直接交出去等于列出这台机器在做的项目——
Claude Code 的窗口是按对话内容命名的。所以要发的话用这个导出，别发原文件。

发到哪：下载站 **http://106.14.76.130** 右下角的咨询窗口，可以带文字和截图。

## 授权

MIT 开源，免费，可商用。源码与发布：
`https://github.com/zhh198903-ctrl/codex-cc-auto-continue`


## 3.1.0 Control integration

本版本新增供 Control 使用的 Codex 本地接口：会话、操作回执、上下文及事件流。
接口只监听本机环回并校验请求令牌；读取会话正文仍需单独开启。
按完整会话标识区分同名会话，多窗口显示同一会话时避免重复发送。
Control 接管期间暂停相应自动批准及权限模式改动，按既有配置保留恢复与续接；
操作结果不确定时暂停目标，不通过重复发送猜测结果。
技能与程序同版发布。Control 内自带副本随其自己的整包更新。
