import { expect, test, type Page } from "@playwright/test";
import type { RefundProposal, StepEvent, Order } from "../src/types";

const saved = { base_url: "https://models.example.test/v1", model_name: "model-a", timeout_s: 60, version: 1, source: "db", configured: true, has_api_key: true };
const states = new WeakMap<Page, ReturnType<typeof createState>>();
function createState() {
  const delivered = new Date().toISOString();
  const orders: Order[] = [
    { id: "DEMO-001-A", product_name: "无线降噪耳机", amount: 129.99, scenario: "refundable", is_final_sale: false },
    { id: "DEMO-001-B", product_name: "清仓连帽卫衣", amount: 59, scenario: "final_sale", is_final_sale: true },
    { id: "DEMO-001-C", product_name: "轻薄笔记本电脑", amount: 1299, scenario: "high_value", is_final_sale: false },
  ].map(order => ({ ...order, status: "delivered", order_date: delivered, delivered_date: delivered, refunded: false })) as Order[];
  return { orders, proposals: [] as RefundProposal[], steps: [] as StepEvent[], messages: [] as { id: number; role: string; content: string; created_at: string }[], tickets: [] as object[], calls: { chat: 0, confirm: 0, cancel: 0, session: 0 }, failConfirmation: false };
}

test.beforeEach(async ({ page }) => {
  const state = createState(); states.set(page, state);
  await page.addInitScript(() => localStorage.setItem("zhixu.theme", "light"));
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url()); const path = url.pathname;
    const json = (value: unknown, status = 200) => route.fulfill({ status, json: value });
    if (path === "/api/meta") return json({ auth_mode: "dev", local_workspace: true, model: "openai-compatible", model_name: saved.model_name, demo: null, refund_policy: { return_window_days: 30, escalation_threshold: 500 } });
    if (path === "/api/local/session") { state.calls.session += 1; return json({ customer: { id: "DEMO-001", name: "林晓" }, orders: state.orders, token: "test-customer", expires_in: 28800 }); }
    if (path === "/api/dev/token") return json({ token: "test-admin", role: "admin" });
    if (path === "/api/me/orders") return json(state.orders);
    if (path === "/api/me/escalations") return json(state.tickets);
    if (path === "/api/me/refund-proposals") return json(state.proposals);
    if (path.endsWith("/stream")) return route.fulfill({ contentType: "text/event-stream", body: ": ping\n\n" });
    if (path.startsWith("/api/me/conversations/")) return json({ id: "conv-ui", customer_id: "DEMO-001", customer_name: "林晓", messages: state.messages, events: state.steps });
    if (path === "/api/chat") {
      state.calls.chat += 1;
      const body = route.request().postDataJSON();
      const order = state.orders.find(o => o.id === body.order_id)!;
      state.proposals.forEach(p => { if (p.status === "pending") p.status = "superseded"; });
      state.messages.push({ id: state.messages.length + 1, role: "user", content: body.message, created_at: new Date().toISOString() });
      const events: object[] = [{ kind: "conversation", conversation_id: "conv-ui" }];
      const addStep = (type: StepEvent["step_type"], payload: Record<string, unknown>) => {
        const step = { kind: "step" as const, id: state.steps.length + 1, seq: state.steps.length + 1, step_type: type, node: "tools", payload, created_at: new Date().toISOString() };
        state.steps.push(step); events.push(step);
      };
      addStep("tool_call", { tool: "get_order", args: { order_id: order.id } });
      addStep("policy_eval", { tool: "check_refund_eligibility", result: { decision: order.is_final_sale ? "denied" : order.amount > 500 ? "escalated" : "approved", reasons: [order.is_final_sale ? "特价商品不可退款。" : "符合基础退款条件。"] } });
      let reply = order.is_final_sale ? "这笔订单属于特价商品，不符合退款条件。" : "这笔订单符合退款条件，咨询不会执行退款。";
      if (String(body.message).includes("申请") && !order.is_final_sale) {
        const proposal: RefundProposal = { id: `rp-${state.proposals.length + 1}`, conversation_id: "conv-ui", order_id: order.id, product_name: order.product_name, amount: order.amount, action: order.amount > 500 ? "review" : "refund", status: "pending", reasons: [order.amount > 500 ? "金额超过 500 元，需要人工审核。" : "在退货期内、非特价，符合自动退款条件。"], created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 900000).toISOString(), refund_id: null };
        state.proposals.push(proposal); addStep("proposal", { tool: "prepare_refund", result: { proposal } });
        events.push({ kind: "refund_proposal", proposal }); reply = "已准备待确认申请，请核对卡片并点击按钮。确认前不会执行。";
      }
      const message = { id: state.messages.length + 1, role: "assistant", content: reply, created_at: new Date().toISOString() };
      state.messages.push(message); events.push({ kind: "message", role: "assistant", content: reply, message_id: message.id }, { kind: "done" });
      return route.fulfill({ contentType: "text/event-stream", body: events.map(event => `data: ${JSON.stringify(event)}\n\n`).join("") });
    }
    if (path.startsWith("/api/me/refund-proposals/") && path.endsWith("/decision")) {
      const proposal = state.proposals.find(p => p.id === path.split("/")[4])!;
      const action = route.request().postDataJSON().decision;
      state.calls[action === "confirm" ? "confirm" : "cancel"] += 1;
      if (state.failConfirmation) { proposal.status = "invalidated"; return json({ detail: "订单条件已变化，请重新确认。" }, 409); }
      proposal.status = action === "cancel" ? "cancelled" : "confirmed";
      if (action === "confirm" && proposal.action === "refund") state.orders.find(o => o.id === proposal.order_id)!.refunded = true;
      if (action === "confirm" && proposal.action === "review") state.tickets.push({ id: 7, order_id: proposal.order_id, amount: proposal.amount, status: "pending", reason: proposal.reasons.join(" "), resolution_note: "" });
      const content = action === "cancel" ? "已取消本次申请，订单状态未改变。" : proposal.action === "review" ? "已提交人工审核，尚未退款。" : "退款已记录，订单状态已更新。";
      const message = { id: state.messages.length + 1, role: "assistant", content, created_at: new Date().toISOString() }; state.messages.push(message);
      const step: StepEvent = { kind: "step", id: state.steps.length + 1, seq: state.steps.length + 1, step_type: "confirmation", node: "customer_action", payload: { status: proposal.status, order_id: proposal.order_id, result: { decision: action === "cancel" ? "cancelled" : proposal.action === "review" ? "escalated" : "approved" } }, created_at: new Date().toISOString() }; state.steps.push(step);
      return json({ proposal, message: { kind: "message", role: "assistant", content, message_id: message.id }, step, replayed: false });
    }
    if (path === "/api/admin/model-config") return json(saved);
    if (path === "/api/admin/model-config/presets") return json([]);
    if (path === "/api/admin/model-config/models") return json({ models: ["model-b", "model-c"] });
    return json({ detail: "没有测试数据" }, 404);
  });
});

test("one fixed customer and three orders replace role switching", async ({ page }) => {
  await page.goto("/chat");
  await expect(page.getByRole("heading", { name: "先查清楚，再由你确认。" })).toBeVisible();
  await expect(page.locator(".workspace-topbar")).toContainText("林晓");
  await expect(page.locator(".scenario-order")).toHaveCount(3);
  await expect(page.getByLabel("以客户身份登录")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /登录管理|退出管理/ })).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "主导航" }).getByRole("link")).toHaveCount(2);
  await expect(page.getByLabel("当前退款规则")).toContainText("500 元");
  await page.screenshot({ path: "test-results/refund-workspace-desktop.png" });
});

test("consultation leaves the order unchanged and does not produce a confirmation", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: "先咨询能否退款", exact: true }).click();
  await expect(page.getByText("这笔订单符合退款条件，咨询不会执行退款。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认退款", exact: true })).toHaveCount(0);
  expect(states.get(page)!.orders[0].refunded).toBe(false);
  expect(states.get(page)!.calls.confirm).toBe(0);
  await expect(page.locator(".refund-trace")).toContainText("符合退款条件");
});

test("event view defaults to skipped original numbers and can show every recorded event", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: "先咨询能否退款", exact: true }).click();
  await expect(page.getByText("这笔订单符合退款条件，咨询不会执行退款。", { exact: true })).toBeVisible();
  const state = states.get(page)!;
  const rows: Pick<StepEvent, "step_type" | "payload">[] = [
    { step_type: "tool_call", payload: { tool: "get_order", args: { order_id: "DEMO-001-A" } } },
    { step_type: "tool_call", payload: { tool: "check_refund_eligibility" } },
    { step_type: "usage", payload: { input_tokens: 100, output_tokens: 20 } },
    { step_type: "tool_result", payload: { tool: "get_order", result: { order_id: "DEMO-001-A" } } },
    { step_type: "policy_eval", payload: { result: { decision: "approved" } } },
    { step_type: "tool_call", payload: { tool: "prepare_refund" } },
    { step_type: "usage", payload: { input_tokens: 200, output_tokens: 30 } },
    { step_type: "proposal", payload: { result: { message: "待确认申请已准备。" } } },
    { step_type: "model", payload: { text: "请核对确认卡片中的订单与金额。" } },
    { step_type: "trace", payload: { trace_id: "trace-example-10" } },
    { step_type: "confirmation", payload: { order_id: "DEMO-001-A", status: "cancelled" } },
  ];
  state.steps = rows.map((row, index) => ({ ...row, kind: "step", id: index + 1, seq: index + 1, node: "test", created_at: new Date().toISOString() }));
  await page.reload();
  const trace = page.locator(".refund-trace");
  const toggle = trace.getByRole("checkbox", { name: "显示全部事件", exact: true });
  const numbers = trace.getByText(/^#\d+$/);
  const compactNumbers = [1, 2, 4, 5, 6, 8, 11].map(n => `#${n}`);
  const allNumbers = rows.map((_, index) => `#${index + 1}`);
  await expect(toggle).not.toBeChecked();
  await expect(numbers).toHaveText(compactNumbers);
  await expect(trace.getByText("模型输出", { exact: true })).toHaveCount(0);
  await toggle.check();
  await expect(numbers).toHaveText(allNumbers);
  await expect(trace).toContainText("请核对确认卡片中的订单与金额。");
  await expect(trace).toContainText("输入 100 · 输出 20");
  await expect(trace).toContainText("trace-example-10");
  await page.screenshot({ path: "test-results/trace-all-events-desktop.png" });
  await page.getByRole("link", { name: "模型设置", exact: true }).click();
  await page.getByRole("link", { name: "退款工作台", exact: true }).click();
  await expect(toggle).toBeChecked();
  await expect(numbers).toHaveText(allNumbers);
  await page.reload();
  await expect(toggle).toBeChecked();
  await expect(numbers).toHaveText(allNumbers);
  await toggle.uncheck();
  await expect(numbers).toHaveText(compactNumbers);
  await page.reload();
  await expect(toggle).not.toBeChecked();
  await expect(numbers).toHaveText(compactNumbers);
  expect(state.calls.chat).toBe(1);
  expect(state.calls.confirm).toBe(0);
});

test("a refund needs a button click and confirmation never calls the model again", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: "发起退款申请", exact: true }).click();
  const card = page.getByRole("article", { name: "退款确认 DEMO-001-A", exact: true });
  await expect(card).toContainText("129.99 元"); await expect(card).toContainText("无线降噪耳机");
  expect(states.get(page)!.orders[0].refunded).toBe(false);
  await expect(page.locator(".refund-trace")).toContainText("等待客户确认");
  await page.screenshot({ path: "test-results/refund-awaiting-confirmation-desktop.png" });
  const calls = states.get(page)!.calls.chat;
  const composer = await page.getByRole("button", { name: "发送消息", exact: true }).boundingBox();
  expect(composer && composer.y + composer.height).toBeLessThanOrEqual(900);
  await card.getByRole("button", { name: "确认退款", exact: true }).click();
  await expect(page.getByText("退款已记录，订单状态已更新。", { exact: true })).toBeVisible();
  expect(states.get(page)!.orders[0].refunded).toBe(true);
  expect(states.get(page)!.calls.chat).toBe(calls);
  expect(states.get(page)!.calls.confirm).toBe(1);
  await expect(page.locator(".refund-trace")).toContainText("退款已记录");
  await expect(card.getByRole("button")).toHaveCount(0);
});

test("cancel is a fixed action and never writes a refund", async ({ page }) => {
  await page.goto("/chat"); await page.getByRole("button", { name: "发起退款申请", exact: true }).click();
  await page.getByRole("button", { name: "取消申请", exact: true }).click();
  await expect(page.getByText("你已取消本次申请", { exact: true })).toBeVisible();
  await expect(page.locator(".refund-trace")).toContainText("已取消申请");
  expect(states.get(page)!.orders[0].refunded).toBe(false); expect(states.get(page)!.calls.chat).toBe(1);
});

test("denied orders do not offer a refund button", async ({ page }) => {
  await page.goto("/chat"); await page.getByRole("button", { name: /特价不可退/ }).click();
  await page.getByRole("button", { name: "发起退款申请", exact: true }).click();
  await expect(page.getByText("这笔订单属于特价商品，不符合退款条件。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认退款", exact: true })).toHaveCount(0);
});

test("a large refund confirms submission for human review on the same page", async ({ page }) => {
  await page.goto("/chat"); await page.getByRole("button", { name: /大额转人工/ }).click();
  await page.getByRole("button", { name: "发起退款申请", exact: true }).click();
  await expect(page.locator(".refund-trace")).toContainText("等待客户确认");
  await expect(page.locator(".refund-trace")).not.toContainText("人工工单已提交");
  await page.getByRole("button", { name: "确认提交人工审核", exact: true }).click();
  await expect(page.getByRole("article", { name: "人工审核进度" })).toContainText("尚未退款");
  expect(states.get(page)!.orders[2].refunded).toBe(false); expect(states.get(page)!.calls.chat).toBe(1);
  await expect(page.locator(".refund-trace")).toContainText("人工工单已提交");
  await expect(page).toHaveURL(/\/chat$/);
});

test("refresh and opening settings preserve the pending card and conversation", async ({ page }) => {
  await page.goto("/chat"); await page.getByRole("button", { name: "发起退款申请", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认退款", exact: true })).toBeVisible();
  await page.reload(); await expect(page.getByRole("button", { name: "确认退款", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "模型设置", exact: true }).click();
  await expect(page.getByLabel("服务地址", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "退款工作台", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认退款", exact: true })).toBeVisible();
  expect(states.get(page)!.calls.chat).toBe(1); expect(states.get(page)!.calls.session).toBe(1);
});

test("changing orders cancels a pending proposal without another model call", async ({ page }) => {
  await page.goto("/chat"); await page.getByRole("button", { name: "发起退款申请", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认退款", exact: true })).toBeVisible();
  await page.getByRole("button", { name: /特价不可退/ }).click();
  await expect(page.getByRole("article", { name: "退款确认 DEMO-001-A", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "发起退款申请", exact: true })).toBeVisible();
  expect(states.get(page)!.calls.cancel).toBe(1); expect(states.get(page)!.calls.chat).toBe(1);
});

test("changed server state disables the old confirmation card", async ({ page }) => {
  await page.goto("/chat"); await page.getByRole("button", { name: "发起退款申请", exact: true }).click();
  states.get(page)!.failConfirmation = true;
  await page.getByRole("button", { name: "确认退款", exact: true }).click();
  await expect(page.getByText("订单或条件已变化，请重新核对", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认退款", exact: true })).toHaveCount(0);
  expect(states.get(page)!.orders[0].refunded).toBe(false);
});

test("typing confirmation in chat never authorizes execution", async ({ page }) => {
  await page.goto("/chat"); await page.getByRole("button", { name: "发起退款申请", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认退款", exact: true })).toBeVisible();
  await page.getByRole("textbox", { name: "消息内容", exact: true }).fill("确认");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.getByText("已有新的问题或申请，此卡片已失效", { exact: true })).toBeVisible();
  expect(states.get(page)!.calls.confirm).toBe(0);
  expect(states.get(page)!.orders[0].refunded).toBe(false);
});

test("an expired card is restored as non-actionable", async ({ page }) => {
  await page.goto("/chat"); await page.getByRole("button", { name: "发起退款申请", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认退款", exact: true })).toBeVisible();
  states.get(page)!.proposals[0].expires_at = new Date(Date.now() - 1000).toISOString();
  states.get(page)!.proposals[0].status = "expired";
  await page.reload();
  await expect(page.getByText("确认已过期，请重新发起", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认退款", exact: true })).toHaveCount(0);
  expect(states.get(page)!.calls.confirm).toBe(0);
});

test("fetch models, choose one and save without typing a model id", async ({
  page,
}) => {
  let submitted: Record<string, unknown> | undefined;
  let activeModel = "model-a";
  await page.route("**/api/meta", (route) =>
    route.fulfill({
      json: {
        auth_mode: "dev",
        model: "openai-compatible",
        model_name: activeModel,
        demo: null,
      },
    }),
  );
  await page.route("**/api/admin/model-config", async (route) => {
    if (route.request().method() === "PUT") {
      submitted = route.request().postDataJSON();
      activeModel = String(submitted!.model_name);
    }
    return route.fulfill({
      json: {
        ...saved,
        model_name: activeModel,
        base_url: String(submitted?.base_url || saved.base_url),
      },
    });
  });
  await page.goto("/settings");
  await page
    .getByLabel("服务地址", { exact: true })
    .fill("https://new.example.test/v1");
  await page.getByLabel("API Key", { exact: true }).fill("test-key");
  await expect(page.getByLabel("模型名称", { exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "获取模型", exact: true }).click();
  await page.getByLabel("模型名称", { exact: true }).selectOption("model-c");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.locator(".workspace-topbar")).toContainText("model-c");
  expect(submitted).toMatchObject({
    base_url: "https://new.example.test/v1",
    model_name: "model-c",
    api_key: "test-key",
  });
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue("");
  await page
    .getByLabel("服务地址", { exact: true })
    .fill("https://another.example.test/v1");
  await expect(page.getByLabel("模型名称", { exact: true })).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "保存", exact: true }),
  ).toBeDisabled();
});

test("a late model list cannot overwrite a changed endpoint", async ({
  page,
}) => {
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/admin/model-config/models", async (route) => {
    await pending;
    await route.fulfill({ json: { models: ["old-endpoint-model"] } });
  });
  await page.goto("/settings");
  const requestStarted = page.waitForRequest(
    "**/api/admin/model-config/models",
  );
  await page.getByRole("button", { name: "获取模型", exact: true }).click();
  await requestStarted;
  await page
    .getByLabel("服务地址", { exact: true })
    .fill("https://new.example.test/v1");
  release!();
  await expect(
    page.getByRole("button", { name: "获取模型", exact: true }),
  ).toBeEnabled();
  await expect(page.getByLabel("模型名称", { exact: true })).toBeDisabled();
  await expect(page.getByLabel("模型名称", { exact: true })).toHaveValue("");
});

test("desktop cards respond to the pointer and reduced motion remains usable", async ({
  page,
}) => {
  await page.goto("/chat");
  const card = page.locator(".motion-card[data-tilt]").first();
  await expect(card).toBeVisible();
  await expect
    .poll(() =>
      card.evaluate((el) =>
        el.getAnimations().every((a) => a.playState === "finished"),
      ),
    )
    .toBe(true);
  await card.hover({ position: { x: 24, y: 24 } });
  await expect(card).toHaveAttribute("data-spotlight-active", "true");
  await expect
    .poll(() => card.evaluate((el) => getComputedStyle(el).transform))
    .not.toBe("none");
  await page.screenshot({
    path: "test-results/chat-hover-desktop.png",
    animations: "disabled",
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.mouse.move(1, 1);
  await card.hover();
  await expect
    .poll(() => card.evaluate((el) => getComputedStyle(el).transform))
    .toBe("none");
  await expect(card).not.toHaveAttribute("data-spotlight-active", "true");
  await expect(page.getByLabel("当前退款规则")).toContainText("500 元");
});
