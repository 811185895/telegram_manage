你这个 `groups = await container.locator(DATE_GROUP_SELECTOR).all()` 出来 `[]`，**最常见原因不是 Telegram 没有 `.message-date-group`，而是你前面定位到的 `container` 根本不是“聊天消息列表”那个 `.messages-container`**（通常是 `MESSAGE_LIST_ROOT_SELECTOR` 太“死”，导致 `.first` 取到了 0 个匹配里的“空 locator”，后面 `.locator()` 都会安静地返回 0）。你脚本里确实是这样写的：先用 `MESSAGE_LIST_ROOT_SELECTOR` 再 `.locator(".messages-container")`。

从你保存的 `pua1.html` 结构看，消息区确实有 `.messages-container` 和 `.message-date-group`（比如 `message-date-group first-message-date-group`）。但 **MessageList 的 class 组合并不总是你写死的那一套**：同一份 html 里还出现了另一个 MessageList：`Transition MessageList custom-scroll with-default-bg scrolled`（少了 `no-avatars/no-composer` 等）。这就会导致你这个选择器在真实页面里经常匹配不到或匹配到别的 Transition。

---

## 直接修：把 MessageList 选择器改“稳”，并加 wait

### 1）把根 selector 改成“找得到 messages-container 的 MessageList”

推荐这样写（**不要把 class 写死**）：

```python
# 更稳：只要是 MessageList 且内部有 messages-container 就行
MESSAGE_LIST_ROOT_SELECTOR = "div.Transition.MessageList:has(div.messages-container)"
MESSAGE_LIST_SELECTOR = f"{MESSAGE_LIST_ROOT_SELECTOR} div.messages-container"
```

然后 `run_once` 里用 `MESSAGE_LIST_SELECTOR` 直接拿 container，别再通过 `.first` 的 message_list 再取子节点：

```python
async def run_once(page: Page, max_groups: Optional[int] = None, scroll_times: int = 5):
    # 强烈建议恢复这个，至少确保容器可见
    if not await ensure_message_list_visible(page):
        return

    scroll_parent = MESSAGE_LIST_ROOT_SELECTOR
    container = page.locator(MESSAGE_LIST_SELECTOR).first

    # 等到至少出现一个日期组（否则 all() 当然是空）
    await container.locator(DATE_GROUP_SELECTOR).first.wait_for(state="attached", timeout=15000)

    processed_group_ids: set[str] = set()
    total_group_index = 0

    for scroll_round in range(scroll_times):
        groups = await container.locator(DATE_GROUP_SELECTOR).all()
        log(f"round={scroll_round} groups={len(groups)}")  # 你可以先打印确认

        for group_el in groups:
            ...
        await scroll_once_to_load_more(page, scroll_parent)
```

### 2）把 `ensure_message_list_visible` 的 selector 也换成新的

你现在 `ensure_message_list_visible` 用的是老的 `MESSAGE_LIST_SELECTOR`（写死 class 组合），而且你在 `run_once` 里把它注释掉了。建议两处都修：

```python
async def ensure_message_list_visible(page: Page) -> bool:
    try:
        container = page.locator(MESSAGE_LIST_SELECTOR).first
        await container.wait_for(state="visible", timeout=15000)
        return True
    except PlaywrightTimeoutError:
        log("未找到消息列表容器，请确认已打开对应频道并登录")
        return False
```

---

## 如果你还想更“保险”：限定在“当前激活 slide”里找

有时页面上同时存在多个 MessageList（比如后台 slide、或其他面板），你可以加一层 `Transition_slide-active` 但别写死 MessageList 的 class：

```python
MESSAGE_LIST_ROOT_SELECTOR = ".Transition_slide-active div.Transition.MessageList:has(div.messages-container)"
MESSAGE_LIST_SELECTOR = f"{MESSAGE_LIST_ROOT_SELECTOR} div.messages-container"
```

---

## 一句定位法（你现在就能验证是不是“选错了容器”）

在 `groups = ...` 前加这两行：

```python
log(f"msglist_root_count={await page.locator(MESSAGE_LIST_ROOT_SELECTOR).count()}")
log(f"container_count={await page.locator(MESSAGE_LIST_SELECTOR).count()}")
log(f"date_group_count={await page.locator(MESSAGE_LIST_SELECTOR + ' ' + DATE_GROUP_SELECTOR).count()}")
```

* 如果 `date_group_count > 0` 但你 `container.locator(...).all()` 还是空，基本就是你 `container` 不是同一个节点（选错了 `.first` / 选到了隐藏的那个）。
* 如果 `date_group_count == 0`，那就是页面还没渲染到消息区（需要 wait / 没跳到目标频道 / 还在入口页）。

---

如果你把上面两处 selector 改掉 + 恢复 `ensure_message_list_visible`，`groups` 基本就会立刻从 `[]` 变成有数据。你要是愿意贴一下你运行时打印出来的三行 count（root/container/date_group），我可以直接告诉你是哪一种情况（选错容器 vs 还没加载到消息列表）。
