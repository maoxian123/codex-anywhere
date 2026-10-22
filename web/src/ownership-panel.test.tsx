import "./test-dom";
import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import OwnershipPanel from "./OwnershipPanel";
import type { ThreadOwnership } from "./types";

afterEach(cleanup);
const managed: ThreadOwnership = {
  state: "managed",
  owners: [],
  can_terminate: false,
  owner_token: null,
};
const external: ThreadOwnership = {
  state: "external",
  owners: [{ name: "codex.exe", pid: 22544 }],
  owner_kind: "candidate",
  can_terminate: true,
  owner_token: "first-identity",
  reason:
    "Windows identified one Codex resource user, not a proven lock holder. Ending this entire process may interrupt its other conversations",
};
function props(ownership: ThreadOwnership | null = managed) {
  return {
    ownership,
    pending: false,
    error: "",
    notice: "",
    onCheck: () => {},
    onTakeover: async (_confirmed?: boolean) => {},
    onRelease: () => {},
    completionIdleRelease: true,
  };
}
function open() {
  const trigger = screen.getByRole("button", { name: /^会话托管：/ });
  fireEvent.click(trigger);
  return trigger;
}

test("ownership stays in one collapsed title button until explicitly opened", () => {
  render(<OwnershipPanel {...props()} />);
  const trigger = screen.getByRole("button", { name: "会话托管：已托管" });
  assert.equal(trigger.getAttribute("aria-expanded"), "false");
  assert.equal(screen.queryByRole("dialog"), null);
  assert.equal(screen.queryByRole("button", { name: "释放托管" }), null);
  open();
  const panel = screen.getByRole("dialog", { name: "会话托管" });
  assert.equal(trigger.getAttribute("aria-expanded"), "true");
  assert.equal(trigger.getAttribute("aria-controls"), panel.id);
  assert.ok(
    screen.getByText(
      /最后一轮结束后 2 分钟无新消息自动释放；运行中和待审批保留托管/,
    ),
  );
  assert.ok(screen.getByRole("button", { name: "释放托管" }));
});

test("older nodes explain that completion-based release needs a node update", () => {
  render(<OwnershipPanel {...props()} completionIdleRelease={false} />);
  open();
  assert.ok(screen.getByText(/当前节点尚未支持完成后自动释放，请更新节点/));
  assert.equal(screen.queryByText(/最后一轮结束后 2 分钟/), null);
});

test("popover closes from outside or Escape and restores keyboard focus", () => {
  render(<OwnershipPanel {...props()} />);
  const trigger = open();
  fireEvent.keyDown(document, { key: "Escape" });
  assert.equal(screen.queryByRole("dialog"), null);
  assert.equal(document.activeElement, trigger);
  open();
  fireEvent.pointerDown(document.body);
  assert.equal(screen.queryByRole("dialog"), null);
});

test("native ownership updates change the closed status without opening or taking over", () => {
  let takeovers = 0;
  const initial = {
    ...props(),
    onTakeover: async () => {
      takeovers += 1;
    },
  };
  const view = render(<OwnershipPanel {...initial} />);
  view.rerender(<OwnershipPanel {...initial} ownership={external} />);
  assert.ok(screen.getByRole("button", { name: "会话托管：其他进程占用" }));
  assert.equal(screen.queryByRole("dialog"), null);
  assert.equal(takeovers, 0);
});

test("candidate processes keep the PID, uncertainty and interruption warning inside details", () => {
  render(<OwnershipPanel {...props(external)} />);
  assert.equal(screen.queryByText(/PID 22544/), null);
  open();
  assert.ok(screen.getByText("PID 22544"));
  assert.ok(screen.getByText(/尚未确认持锁/));
  assert.ok(screen.getByText(/无法证明它是实际持锁者/));
  assert.ok(screen.getByText(/中断其承载的其他会话/));
});

test("dangerous takeover keeps explicit confirmation and invalidates a changed owner token", () => {
  const calls: (boolean | undefined)[] = [];
  const initial = {
    ...props(external),
    onTakeover: async (confirmed?: boolean) => {
      calls.push(confirmed);
    },
  };
  const view = render(<OwnershipPanel {...initial} />);
  open();
  fireEvent.click(screen.getByRole("button", { name: "结束进程并接管" }));
  assert.deepEqual(calls, []);
  assert.ok(screen.getByRole("heading", { name: "结束占用进程并接管" }));
  assert.ok(screen.getByRole("dialog"));
  view.rerender(
    <OwnershipPanel
      {...initial}
      ownership={{ ...external, owner_token: "different-identity" }}
    />,
  );
  assert.equal(
    (
      screen.getByRole("button", {
        name: "确认结束并接管",
      }) as HTMLButtonElement
    ).disabled,
    true,
  );
  fireEvent.click(screen.getByRole("button", { name: "确认结束并接管" }));
  assert.deepEqual(calls, []);
});

test("only a verified external identity offers process termination", () => {
  const view = render(
    <OwnershipPanel {...props({ ...external, owner_token: null })} />,
  );
  open();
  assert.equal(screen.queryByRole("button", { name: "结束进程并接管" }), null);
  view.rerender(
    <OwnershipPanel {...props({ ...external, can_terminate: false })} />,
  );
  assert.equal(screen.queryByRole("button", { name: "结束进程并接管" }), null);
});

test("cancelling or closing process confirmation restores focus to the title control", () => {
  const calls: (boolean | undefined)[] = [];
  render(
    <OwnershipPanel
      {...props(external)}
      onTakeover={async (confirmed) => {
        calls.push(confirmed);
      }}
    />,
  );
  for (const closeButton of ["取消", "关闭窗口"]) {
    const trigger = open();
    const takeover = screen.getByRole("button", { name: "结束进程并接管" });
    takeover.focus();
    fireEvent.click(takeover);
    const close = screen.getByRole("button", { name: closeButton });
    close.focus();
    fireEvent.click(close);
    assert.equal(screen.queryByRole("dialog"), null);
    assert.equal(document.activeElement, trigger);
    assert.equal(trigger.getAttribute("aria-expanded"), "false");
  }
  assert.deepEqual(calls, []);
});

test("closed errors and release notices remain announced and available in the trigger", () => {
  const view = render(<OwnershipPanel {...props()} error="节点无法释放托管" />);
  const trigger = screen.getByRole("button", {
    name: "会话托管：已托管，需处理",
  });
  assert.equal(trigger.title, "节点无法释放托管");
  assert.match(
    screen.getByRole("status").textContent || "",
    /节点无法释放托管/,
  );
  assert.equal(screen.queryByRole("dialog"), null);
  open();
  assert.match(screen.getByRole("alert").textContent || "", /节点无法释放托管/);
  fireEvent.keyDown(document, { key: "Escape" });
  view.rerender(
    <OwnershipPanel {...props()} notice="当前任务仍在执行，暂不释放托管" />,
  );
  assert.equal(
    screen.getByRole("button", { name: "会话托管：已托管，有提示" }).title,
    "当前任务仍在执行，暂不释放托管",
  );
  assert.equal(screen.queryByRole("dialog"), null);
});

test("checking remains visible and all mutations stay disabled while pending", () => {
  let checks = 0;
  render(
    <OwnershipPanel
      {...props(external)}
      pending
      onCheck={() => {
        checks += 1;
      }}
    />,
  );
  assert.ok(screen.getByLabelText("处理中"));
  open();
  assert.equal(
    (screen.getByRole("button", { name: "重新检查" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  assert.equal(
    (
      screen.getByRole("button", {
        name: "结束进程并接管",
      }) as HTMLButtonElement
    ).disabled,
    true,
  );
  fireEvent.click(screen.getByRole("button", { name: "重新检查" }));
  assert.equal(checks, 0);
});

test("available sessions take over without process termination confirmation", () => {
  const calls: (boolean | undefined)[] = [];
  render(
    <OwnershipPanel
      {...props({ ...managed, state: "available" })}
      onTakeover={async (confirmed) => {
        calls.push(confirmed);
      }}
    />,
  );
  open();
  fireEvent.click(screen.getByRole("button", { name: "接管会话" }));
  assert.deepEqual(calls, [undefined]);
  assert.equal(
    screen.queryByRole("heading", { name: "结束占用进程并接管" }),
    null,
  );
});
